import asyncio
import pytest

from leadgen.scrape import decision_maker_spec, scrape_profile
from leadgen.pipeline.classify import matches_spec
from leadgen.sources.fake import FakeSource


@pytest.mark.asyncio
async def test_company_decision_makers_run(store, run_pipeline):
    spec = decision_maker_spec("northwind-pay", max_people=40, source="fake")
    run = store.create_run(spec)
    result = await run_pipeline(run)
    assert result.status == "done", result.message
    rows = store.list_people(run.id, limit=1000)
    assert rows, "no people enumerated"
    # the company was the single seed
    companies = store.list_companies(run.id)
    assert {c.key for c in companies} == {"northwind-pay"}
    # the scraper targets CPaaS buyer titles and excludes individual developers/engineers
    titles = [(p.raw_title or "").lower() for p, _ in rows]
    assert titles, "no people enumerated"
    assert not any("software engineer" in t or t == "developer" or "devops" in t for t in titles), titles
    # it found contactable decision-makers (sales/product/exec/cx), not ICs
    dm = [(p, c) for p, c in rows if matches_spec(c, spec)]
    assert dm, "no decision-makers matched"


@pytest.mark.asyncio
async def test_scrape_profile_fake():
    src = FakeSource()
    data = await scrape_profile(src, "jane-doe")
    assert data["name"] == "Jane Doe"
    assert data["url"].endswith("/in/jane-doe/")
    assert any("Head of Engineering" in (e.get("title") or "") for e in data["experience"])
    assert "education" in data["sections"] and "skills" in data["sections"]
    # contact info is extracted when LinkedIn exposes it
    c = data["contacts"]
    assert c["available"] is True
    assert c["emails"] == ["jane.doe@northwindpay.example"]
    assert c["phones"] and "+49 170 1234567" in c["phones"][0]
    assert any("janedoe.example" in w for w in c["websites"])
