from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from leadgen.web.app import render
from leadgen.web.routes.common import state

router = APIRouter(prefix="/runs/{run_id}")


@router.get("/companies")
async def companies(request: Request, run_id: str):
    st = state(request)
    run = st.store.get_run(run_id)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    return render(request, "companies.html", run=run, companies=st.store.list_companies(run_id),
                  counts=st.store.company_people_counts(run_id), facets=st.store.list_facets(run_id))
