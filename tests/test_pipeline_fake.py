import asyncio

import pytest

from leadgen.models import Department, Seniority, Spec


def make_spec(**overrides) -> Spec:
    base = {
        "name": "fintech-eu",
        "company": {"industries": ["fintech"], "locations": ["Germany", "Lebanon"], "employee_size": ["51-200", "201-500"],
                     "max_companies": 5},
        "people": {"titles": ["Head of Sales", "CTO"], "departments": [], "max_per_company": 40, "max_pages_per_facet": 3},
        "run": {"source": "fake", "strategy": "company_first", "min_delay_s": 0, "max_delay_s": 0},
    }
    for k, v in overrides.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return Spec.model_validate(base)


@pytest.mark.asyncio
async def test_full_run_on_fake_source(store, run_pipeline, settings):
    run = store.create_run(make_spec())
    result = await run_pipeline(run)
    assert result.status == "done", result.message
    companies = store.list_companies(run.id)
    assert companies, "discovery produced nothing"
    passed = [c for c in companies if c.status == "enumerated"]
    assert passed, [c.filter_reasons for c in companies]
    # the two fintech companies (Berlin / Beirut) pass; others are filtered out
    names = {c.name for c in passed}
    assert "Northwind Pay" in names and "Cedar" in names
    assert all(c.status == "filtered_out" or c.status == "enumerated" for c in companies)
    people = store.list_people(run.id, limit=1000)
    assert len(people) > 20
    classified = [cls for _, cls in people if cls and cls.department != Department.UNCLASSIFIED]
    assert len(classified) / len(people) > 0.85, "rule classifier coverage too low on synthetic titles"
    seniorities = {cls.seniority for _, cls in people if cls}
    assert Seniority.CXO in seniorities and Seniority.MID in seniorities
    out_dir = settings.outputs / run.id
    for name in ("leads.csv", "companies.csv", "leads.xlsx", "report.md", "run.json", "leads.jsonl"):
        assert (out_dir / name).exists(), name
    report = (out_dir / "report.md").read_text()
    assert "Department x seniority" in report
    anonymized = [p for p, _ in people if p.anonymized]
    assert not anonymized, "anonymized members must be dropped by default"


@pytest.mark.asyncio
async def test_seed_companies_skip_discovery(store, run_pipeline):
    spec = make_spec(company={"industries": [], "locations": [], "employee_size": [], "seed_companies": ["quanta-ai"]})
    run = store.create_run(spec)
    result = await run_pipeline(run)
    assert result.status == "done", result.message
    companies = store.list_companies(run.id)
    assert [c.key for c in companies] == ["quanta-ai"]
    assert companies[0].status == "enumerated"
    assert companies[0].industry == "Software Development"
    assert companies[0].urn == "71004"


@pytest.mark.asyncio
async def test_stop_then_resume_keeps_progress(store, pipeline, fake_factory):
    spec = make_spec(company={"seed_companies": ["ferrum-logistics", "solstice-energy"], "industries": [], "locations": [],
                              "employee_size": []}, people={"max_per_company": 60})
    run = store.create_run(spec)
    stop = asyncio.Event()
    orig_update = store.update_facet
    calls = {"n": 0}

    def tripping_update(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            stop.set()
        return orig_update(*a, **kw)

    store.update_facet = tripping_update  # type: ignore[assignment]
    result = await pipeline.execute(run, fake_factory, pause=asyncio.Event(), stop=stop)
    assert result.status == "stopped"
    before = store.count_people(run.id)
    assert before > 0
    store.update_facet = orig_update  # type: ignore[assignment]
    fake_factory.source.calls.clear()
    result2 = await pipeline.execute(store.get_run(run.id), fake_factory, pause=asyncio.Event(), stop=asyncio.Event())
    assert result2.status == "done", result2.message
    assert store.count_people(run.id) >= before
    # discovery and profiling were not repeated
    assert not any(t == "search_companies" for t, _ in fake_factory.source.calls)


def test_plan_estimates(pipeline):
    plan = pipeline.plan(make_spec())
    assert plan["strategy"] == "company_first"
    assert plan["estimate"]["total"] > 0
    assert any("fintech" in q.lower() for q in plan["company_queries"])
    assert any(f["label"].startswith("titles") for f in plan["facets"])


@pytest.mark.asyncio
async def test_people_first_run(store, run_pipeline, settings):
    spec = make_spec(run={"source": "fake", "strategy": "people_first", "min_delay_s": 0, "max_delay_s": 0},
                     company={"industries": ["fintech"], "locations": ["Germany"], "employee_size": [], "max_companies": 10},
                     people={"titles": ["Head of Engineering", "CTO", "Head of Sales"], "max_pages_per_facet": 2})
    run = store.create_run(spec)
    result = await run_pipeline(run)
    assert result.status == "done", result.message
    people = store.list_people(run.id, limit=1000)
    assert people, "people-first produced no leads"
    # every lead carries a company derived from the person
    assert all(p.company_key.startswith("name:") for p, _ in people)
    companies = store.list_companies(run.id)
    assert companies and all(c.status == "enumerated" for c in companies)
    # location was honoured: only German-HQ fake companies contribute, never Beirut/SF/London ones
    from leadgen.sources.fake import COMPANIES
    from leadgen.pipeline.geo import location_country
    german = {c["name"] for c in COMPANIES if location_country(c["hq"]) == "Germany"}
    names = {c.name for c in companies}
    assert names and names <= german, (names, german)
    assert "Cedar" not in names and "Quanta AI" not in names


def test_people_first_plan(pipeline):
    spec = make_spec(run={"source": "fake", "strategy": "people_first"},
                     company={"industries": ["fintech"], "locations": ["Germany"]},
                     people={"titles": ["CTO", "Head of Engineering"]})
    plan = pipeline.plan(spec)
    assert plan["strategy"] == "people_first"
    assert plan["lead_queries"] and all(q["geo_urn"] for q in plan["lead_queries"])
    assert any("fintech" in q["keywords"].lower() for q in plan["lead_queries"])
