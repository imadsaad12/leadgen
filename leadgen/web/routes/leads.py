from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from leadgen.pipeline.classify import matches_spec
from leadgen.titles.grouping import group_people, pivot
from leadgen.web.app import render
from leadgen.web.routes.common import state

router = APIRouter(prefix="/runs/{run_id}")


@router.get("/leads")
async def leads(request: Request, run_id: str, company: str = "", department: str = "", seniority: str = "",
                q: str = "", review: str = "", relevant: str = "", group: str = "department_seniority", page: int = 1, per_page: int = 100):
    st = state(request)
    run = st.store.get_run(run_id)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    needs_review = True if review == "1" else None
    rc = st.store.relevance_counts(run_id)
    scored = (rc.get("relevant", 0) + rc.get("excluded", 0)) > 0
    # default to showing only relevant leads once a relevance filter has run
    rel = {"1": True, "0": False, "": (True if scored and review != "1" else None)}.get(relevant, None)
    total = st.store.count_people(run_id, company_key=company or None, department=department or None,
                                  seniority=seniority or None, q=q or None, needs_review=needs_review, relevant=rel)
    rows = st.store.list_people(run_id, company_key=company or None, department=department or None,
                                seniority=seniority or None, q=q or None, needs_review=needs_review, relevant=rel,
                                limit=per_page, offset=(page - 1) * per_page)
    all_rows = st.store.list_people(run_id, limit=100000)
    companies = {c.key: c for c in st.store.list_companies(run_id)}
    return render(request, "leads.html", run=run, rows=rows, total=total, page=page, per_page=per_page,
                  companies=companies, filters={"company": company, "department": department, "seniority": seniority,
                                                "q": q, "review": review, "relevant": relevant, "group": group},
                  relevance=rc, scored=scored,
                  groups=group_people(all_rows, by=group), pivot=pivot(all_rows, "department", "seniority"),
                  matches_spec=matches_spec)
