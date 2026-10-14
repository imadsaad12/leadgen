from __future__ import annotations

from fastapi import APIRouter, Request

from leadgen.models import RunSpec
from leadgen.store.budget import Budget
from leadgen.store.cache import ToolCache
from leadgen.web.app import render
from leadgen.web.routes.common import state, status_badge

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    st = state(request)
    runs = st.store.list_runs(30)
    budget = Budget(st.db, RunSpec(daily_budget=int(st.db.get_setting("daily_budget", 100)),
                                   monthly_search_cap=int(st.db.get_setting("monthly_search_cap", 250)))).summary()
    sidecar = st.manager.running()
    healthy = await st.manager.healthy() if sidecar else False
    return render(request, "dashboard.html", runs=runs, budget=budget, sidecar=sidecar, healthy=healthy,
                  cache=ToolCache(st.db).stats(), status_badge=status_badge,
                  llm_on=bool(st.db.get_setting("llm_assist", True)),
                  acknowledged=bool(st.db.get_setting("risk_acknowledged", False)))
