"""Post-run relevance filter: Claude scores each lead's fit to the spec's offer and excludes poor fits.

Runs only when an offer is set and the claude CLI is available. Leads are flagged (relevant / fit_score / reason),
never deleted, so exports default to the clean list while the excluded ones stay auditable.
"""

from __future__ import annotations

from leadgen.llm.prompts import RELEVANCE_SYSTEM
from leadgen.llm.schemas import RelevanceBatch
from leadgen.sources.base import RunContext


def _lead_line(i: int, person, cls) -> str:
    dept = cls.department.value if cls else "?"
    sen = cls.seniority.value if cls else "?"
    return (f"{i}. name={person.full_name!r} title={person.raw_title or person.headline or '?'!r} "
            f"company={person.company_name or '?'!r} dept={dept} seniority={sen}")


async def filter_relevance(ctx: RunContext, batch_size: int = 40) -> dict[str, int]:
    """Score every unscored lead against ctx.spec.offer. Returns {relevant, excluded} counts."""
    offer = (ctx.spec.offer or "").strip()
    if not offer or ctx.llm is None:
        return {}
    pending = ctx.store.leads_for_relevance(ctx.run_id)
    if not pending:
        return {}
    from leadgen.pipeline.classify import classify_title

    relevant = excluded = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        ctx.check_cancelled()
        lines = []
        for i, person in enumerate(chunk):
            cls = ctx.store.get_title(person.raw_title and classify_title(person.raw_title, ctx.store).normalized or "")
            lines.append(_lead_line(i, person, cls))
        prompt = f"OFFER:\n{offer}\n\nLEADS:\n" + "\n".join(lines)
        try:
            result = await ctx.llm.structured(prompt, RelevanceBatch, system=RELEVANCE_SYSTEM, kind="relevance")
        except Exception as e:  # noqa: BLE001 - never let the optional filter fail a finished run
            ctx.log(f"relevance filter failed ({e}); leaving leads unscored", "warning")
            break
        ctx.stats.llm_calls += 1
        by_index = {it.index: it for it in result.items}
        for i, person in enumerate(chunk):
            verdict = by_index.get(i)
            if verdict is None:
                continue
            ctx.store.set_relevance(ctx.run_id, person.key, verdict.relevant, verdict.fit_score, verdict.reason)
            if verdict.relevant:
                relevant += 1
            else:
                excluded += 1
        ctx.log(f"relevance: scored {min(start + batch_size, len(pending))}/{len(pending)} "
                f"({relevant} kept, {excluded} excluded so far)")
    return {"relevant": relevant, "excluded": excluded}
