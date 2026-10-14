"""Title classification for a run: overrides -> title cache -> rules -> optional claude CLI batch."""

from __future__ import annotations

from leadgen.llm.prompts import TITLE_SYSTEM
from leadgen.llm.schemas import TitleBatch
from functools import lru_cache

from leadgen.models import Department, Person, Spec, TitleClassification
from leadgen.sources.base import RunContext
from leadgen.store.runs import RunStore
from leadgen.titles.rules import classify_with_overrides, load_overrides, normalize_key, normalize_title


def classify_title(raw_title: str | None, store: RunStore, overrides: dict | None = None) -> TitleClassification:
    if not raw_title:
        return TitleClassification(raw_title="", normalized="", method="none")
    overrides = overrides if overrides is not None else load_overrides()
    cls = classify_with_overrides(raw_title, overrides)
    if cls.method == "override":
        return cls
    cached = store.get_title(cls.normalized) if cls.normalized else None
    if cached is not None and cached.method in ("llm", "manual"):
        return cached.model_copy(update={"raw_title": raw_title, "function_tags": cls.function_tags})
    if cls.method == "rule" and cls.normalized:
        store.put_title(cls)
    return cls


def excluded_by_spec(person: Person, spec: Spec) -> bool:
    title = (person.raw_title or person.headline or "").lower()
    return any(ex.lower() in title for ex in spec.people.exclude_titles if ex)


def _norm_spec_title(t: str) -> str:
    # Expand abbreviations the same way classified titles are, so "CTO" matches "chief technology officer".
    primary, _ = normalize_title(t)
    return primary or normalize_key(t)


@lru_cache(maxsize=256)
def _spec_target_departments(titles: tuple[str, ...]) -> frozenset:
    # The functions the spec's target titles point at, e.g. "Head of Product" -> product.
    from leadgen.titles.rules import classify_rule

    out = set()
    for t in titles:
        c = classify_rule(t)
        if c.department != Department.UNCLASSIFIED:
            out.add(c.department)
    return frozenset(out)


def matches_spec(cls: TitleClassification | None, spec: Spec) -> bool:
    """A lead "matches" when it is a person worth pitching to: the right seniority in a function the spec targets.

    Seniority (if the spec sets levels) is required. The function is satisfied by an explicit department, by a
    department implied by the target titles, or by a direct title hit -- so "Director of Product Management" counts
    for a spec that lists "Head of Product", not only an exact title match.
    """
    if cls is None:
        return False
    ps = spec.people
    if ps.seniorities and cls.seniority not in ps.seniorities:
        return False
    target_depts = set(ps.departments) | _spec_target_departments(tuple(ps.titles))
    if target_depts and cls.department in target_depts:
        return True
    if ps.titles:
        norm = cls.normalized or normalize_key(cls.raw_title)
        hay = f"{norm} {' '.join(cls.function_tags).lower()}"
        if any((st and (st in hay or norm in st)) for st in (_norm_spec_title(t) for t in ps.titles)):
            return True
    # No functional constraint at all -> seniority alone (already checked) is enough.
    return not target_depts and not ps.titles


async def llm_classify_unclassified(ctx: RunContext, batch_size: int = 100) -> int:
    """Send unclassified titles of this run to the claude CLI; returns the number of people updated."""
    if ctx.llm is None:
        return 0
    rows = ctx.store.unclassified_titles(ctx.run_id)
    titles = [r["raw_title"] for r in rows if r["raw_title"]]
    if not titles:
        return 0
    updated = 0
    for i in range(0, len(titles), batch_size):
        chunk = titles[i : i + batch_size]
        ctx.check_cancelled()
        prompt = "Classify these job titles:\n" + "\n".join(f"- {t}" for t in chunk)
        try:
            result = await ctx.llm.structured(prompt, TitleBatch, system=TITLE_SYSTEM, kind="titles")
        except Exception as e:  # ClaudeCLIError or subprocess trouble: keep going without assist
            ctx.log(f"claude title classification failed: {e}", "warning")
            return updated
        ctx.stats.llm_calls += 1
        by_raw = {normalize_key(item.raw_title): item for item in result.items}
        for raw in chunk:
            item = by_raw.get(normalize_key(raw))
            if item is None or item.department == Department.UNCLASSIFIED:
                continue
            base = classify_with_overrides(raw)
            cls = base.model_copy(update={
                "department": item.department, "seniority": item.seniority,
                "canonical_title": item.canonical_title or base.canonical_title, "confidence": 0.8, "method": "llm",
            })
            if cls.normalized:
                ctx.store.put_title(cls)
            for run_id, person_key in ctx.store.people_with_title(raw, ctx.run_id):
                ctx.store.set_classification(run_id, person_key, cls)
                updated += 1
        ctx.log(f"claude classified {len(chunk)} titles ({updated} people updated so far)")
    return updated


def reclassify_run(run_id: str, store: RunStore) -> int:
    """Re-apply overrides/cache/rules to every person of a run without any network calls."""
    overrides = load_overrides()
    n = 0
    for person_key, raw_title, _ in store.all_people_rows(run_id):
        cls = classify_title(raw_title, store, overrides)
        store.set_classification(run_id, person_key, cls)
        n += 1
    return n
