
import pytest

from leadgen.models import RunSpec
from leadgen.sources.linkedin_mcp import ALLOWED_TOOLS, ForbiddenTool, LinkedInMCP
from leadgen.store.budget import Budget, BudgetExhausted, CircuitOpen
from leadgen.store.cache import ToolCache, canonical_key


def test_cache_key_is_order_and_null_independent():
    a = canonical_key("search_people", {"keywords": "x", "page": None, "location": ""})
    b = canonical_key("search_people", {"location": None, "keywords": "x"})
    assert a == b
    assert canonical_key("search_people", {"keywords": "x", "page": 2}) != a


def test_cache_roundtrip_and_purge(db):
    cache = ToolCache(db)
    assert cache.get("search_people", {"keywords": "x"}) is None
    cache.put("search_people", {"keywords": "x"}, {"sections": {"search_results": "hi"}})
    assert cache.get("search_people", {"keywords": "x"})["sections"]["search_results"] == "hi"
    assert cache.hits == 1 and cache.misses == 1
    assert cache.stats()["total"] == 1
    assert cache.purge(tool="search_people") == 1


@pytest.mark.asyncio
async def test_budget_limits_and_circuit_breaker(db):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    b = Budget(db, RunSpec(daily_budget=2, monthly_search_cap=1, min_delay_s=1, max_delay_s=1, break_every=100),
               clock=lambda: 0.0, sleep=fake_sleep)
    await b.acquire("get_company_profile")
    await b.acquire("search_people")
    assert b.today_page_loads() == 2 and b.month_searches() == 1
    with pytest.raises(BudgetExhausted):
        await b.acquire("get_company_profile")  # daily budget
    b2 = Budget(db, RunSpec(daily_budget=100, monthly_search_cap=1, min_delay_s=0, max_delay_s=0), sleep=fake_sleep)
    with pytest.raises(BudgetExhausted):
        await b2.acquire("search_people")  # monthly cap
    await b2.acquire("get_company_profile")  # non-search tools still fine
    for _ in range(3):
        b2.strike("challenge")
    with pytest.raises(CircuitOpen):
        await b2.acquire("get_company_profile")
    assert sleeps and all(s >= 0 for s in sleeps)


@pytest.mark.asyncio
async def test_allowlist_blocks_write_tools():
    mcp = LinkedInMCP("http://127.0.0.1:1/mcp")
    for tool in ("connect_with_person", "send_message", "get_inbox", "search_posts"):
        with pytest.raises(ForbiddenTool):
            await mcp.call(tool, {})
    assert "send_message" not in ALLOWED_TOOLS and "search_people" in ALLOWED_TOOLS


def test_run_store_people_and_titles(store):
    from leadgen.models import Person, Spec, TitleClassification, Department, Seniority

    run = store.create_run(Spec(name="t"))
    p = Person(key="u1", username="u1", profile_url="https://x/in/u1/", full_name="U One", raw_title="Head of Sales",
               company_key="acme")
    cls = TitleClassification(raw_title="Head of Sales", normalized="head of sales", department=Department.SALES,
                              seniority=Seniority.DIRECTOR, confidence=0.9, method="rule")
    store.upsert_person(run.id, p, cls)
    store.upsert_person(run.id, Person(key="u2", username="u2", profile_url="x", full_name="U Two", raw_title="Wizard",
                                       company_key="acme"), TitleClassification(raw_title="Wizard", normalized="wizard"))
    assert store.count_people(run.id) == 2
    assert store.count_people(run.id, department="sales") == 1
    assert [u["raw_title"] for u in store.unclassified_titles(run.id)] == ["Wizard"]
    assert store.group_counts(run.id)[0]["n"] == 1
    store.put_title(cls)
    assert store.get_title("head of sales").department == Department.SALES
    assert store.forget_person("u1") == 1
    assert store.count_people(run.id) == 1
