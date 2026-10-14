
import pytest
from fastapi.testclient import TestClient

import leadgen.config as config
from leadgen.web.app import create_app


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(config, "_settings", settings)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _form(**over):
    base = {
        "name": "web-demo", "source": "fake", "industries": "fintech", "locations_text": "Berlin, Lebanon",
        "employee_size": ["51-200", "201-500"], "titles": "Head of Sales, CTO", "max_companies": "3",
        "max_per_company": "25", "max_pages_per_facet": "2", "daily_budget": "100", "monthly_search_cap": "250",
        "min_delay_s": "0", "max_delay_s": "0", "broad_sweep": "on", "llm_assist": "off", "action": "start",
        "strategy": "company_first",
    }
    base.update(over)
    return base


def _wait_done(client: TestClient, run_id: str, timeout: float = 30.0) -> str:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}/stats")
        if any(s in r.text for s in ("done", "failed", "stopped", "paused")) and "running" not in r.text:
            return r.text
        time.sleep(0.2)
    raise AssertionError("run did not finish in time")


def test_dashboard_and_new_run_pages(client):
    assert client.get("/").status_code == 200
    r = client.get("/runs/new")
    assert r.status_code == 200 and "Start run" in r.text


def test_plan_partial(client):
    r = client.post("/runs/plan", data=_form())
    assert r.status_code == 200 and "Estimated LinkedIn page loads" in r.text


def test_full_fake_run_through_the_ui(client):
    r = client.post("/runs/new", data=_form(), follow_redirects=False)
    assert r.status_code == 303, r.text
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    stats_html = _wait_done(client, run_id)
    assert "done" in stats_html, stats_html
    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200 and "Northwind Pay" in detail.text
    leads = client.get(f"/runs/{run_id}/leads")
    assert leads.status_code == 200 and "Department × seniority" in leads.text
    filtered = client.get(f"/runs/{run_id}/leads?department=sales")
    assert filtered.status_code == 200
    companies = client.get(f"/runs/{run_id}/companies")
    assert companies.status_code == 200 and "Facet progress" in companies.text
    for fmt in ("xlsx", "csv", "report", "jsonl", "json"):
        x = client.get(f"/runs/{run_id}/export?format={fmt}")
        assert x.status_code == 200, fmt
        assert len(x.content) > 100
    titles = client.get(f"/titles?run_id={run_id}")
    assert titles.status_code == 200
    # manual assignment of an unclassified title propagates
    uncl = client.app.state.store.unclassified_titles(run_id)
    if uncl:
        raw = uncl[0]["raw_title"]
        r = client.post("/titles/assign", data={"raw_title": raw, "department": "other", "seniority": "mid", "run_id": run_id},
                        follow_redirects=False)
        assert r.status_code == 303
        assert all(u["raw_title"] != raw for u in client.app.state.store.unclassified_titles(run_id))
    r = client.post(f"/runs/{run_id}/reclassify", follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/settings")
    assert r.status_code == 200 and "LinkedIn sidecar" in r.text
    r = client.post("/settings", data={"daily_budget": "50", "monthly_search_cap": "200", "min_delay_s": "5",
                                       "max_delay_s": "9", "retention_days": "30", "llm_assist": "on"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.db.get_setting("daily_budget") == 50
    with client.stream("GET", f"/runs/{run_id}/events?after=0") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "data:" in body and "event: done" in body


def test_linkedin_run_requires_acknowledgement(client):
    r = client.post("/runs/new", data=_form(source="linkedin"), follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/runs/new", data=_form(source="fake", acknowledge="1", action="save"), follow_redirects=False)
    assert r.status_code == 303 and "spec_id=" in r.headers["location"]
    assert client.app.state.db.get_setting("risk_acknowledged") is True
