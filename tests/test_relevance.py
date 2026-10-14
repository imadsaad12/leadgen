import asyncio

import pytest

from leadgen.llm.schemas import RelevanceBatch
from leadgen.models import Spec
from leadgen.pipeline.relevance import filter_relevance
from leadgen.sources.base import RunContext
from leadgen.store.cache import ToolCache


class StubLLM:
    """Stands in for the claude CLI: marks engineering/exec leads relevant, everyone else excluded."""

    def __init__(self):
        self.calls = 0

    async def structured(self, prompt, schema_model, *, system, kind="generic", use_cache=True):
        self.calls += 1
        items = []
        # parse the numbered lead lines back out of the prompt
        for line in prompt.splitlines():
            line = line.strip()
            if not line or not line[0].isdigit() or "." not in line:
                continue
            idx = int(line.split(".", 1)[0])
            low = line.lower()
            good = any(k in low for k in ("dept=engineering", "dept=executive", "dept=product", "cto", "head of engineering"))
            items.append({"index": idx, "relevant": good, "fit_score": 90 if good else 10,
                          "reason": "owns the relevant function" if good else "not a buyer for this offer"})
        return RelevanceBatch.model_validate({"items": items})


@pytest.mark.asyncio
async def test_relevance_filter_excludes_poor_fits(store, events, db):
    from leadgen.models import Person, TitleClassification, Department, Seniority

    spec = Spec.model_validate({"name": "t", "offer": "A CPaaS messaging API for engineering/product leaders."})
    run = store.create_run(spec)
    people = [
        ("u1", "Jane CTO", "CTO", Department.ENGINEERING, Seniority.CXO),
        ("u2", "Max Eng", "Head of Engineering", Department.ENGINEERING, Seniority.DIRECTOR),
        ("u3", "Sam Intern", "Marketing Intern", Department.MARKETING, Seniority.INTERN),
        ("u4", "Lee Student", "Student", Department.UNCLASSIFIED, Seniority.UNKNOWN),
    ]
    for key, name, title, dep, sen in people:
        p = Person(key=key, username=key, profile_url="x", full_name=name, raw_title=title, company_key="name:acme",
                   company_name="Acme")
        cls = TitleClassification(raw_title=title, normalized=title.lower(), department=dep, seniority=sen)
        store.upsert_person(run.id, p, cls)
    assert store.relevance_counts(run.id).get("unscored") == 4

    llm = StubLLM()
    ctx = RunContext(run_id=run.id, spec=spec, store=store, cache=ToolCache(db), events=events, llm=llm)
    counts = await filter_relevance(ctx)
    assert counts == {"relevant": 2, "excluded": 2}, counts
    rc = store.relevance_counts(run.id)
    assert rc.get("relevant") == 2 and rc.get("excluded") == 2 and "unscored" not in rc

    # exports: leads.csv holds only the relevant, excluded.csv the rest
    from leadgen.export.report import write_all_exports
    out = db  # not used
    import pathlib, tempfile, csv
    d = pathlib.Path(tempfile.mkdtemp())
    paths = write_all_exports(run.id, store, d)
    kept = list(csv.DictReader(open(paths["leads_csv"])))
    exc = list(csv.DictReader(open(paths["excluded_csv"])))
    assert {r["full_name"] for r in kept} == {"Jane CTO", "Max Eng"}
    assert {r["full_name"] for r in exc} == {"Sam Intern", "Lee Student"}
    assert all(int(r["fit_score"]) >= 90 for r in kept)


@pytest.mark.asyncio
async def test_relevance_noop_without_offer(store, events, db):
    spec = Spec.model_validate({"name": "t", "offer": ""})
    run = store.create_run(spec)
    ctx = RunContext(run_id=run.id, spec=spec, store=store, cache=ToolCache(db), events=events, llm=StubLLM())
    assert await filter_relevance(ctx) == {}
