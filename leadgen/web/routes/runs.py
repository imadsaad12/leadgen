from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from leadgen.models import Spec
from leadgen.web.app import render
from leadgen.web.routes.common import spec_from_form, state, status_badge

router = APIRouter(prefix="/runs")


def _default_spec(st) -> Spec:
    s = Spec(name="new-run")
    s.run.daily_budget = int(st.db.get_setting("daily_budget", 100))
    s.run.monthly_search_cap = int(st.db.get_setting("monthly_search_cap", 250))
    s.run.min_delay_s = float(st.db.get_setting("min_delay_s", 8.0))
    s.run.max_delay_s = float(st.db.get_setting("max_delay_s", 20.0))
    return s


@router.get("/new")
async def new_run(request: Request, spec_id: str | None = None, clone: str | None = None):
    st = state(request)
    spec = _default_spec(st)
    if spec_id:
        spec = st.store.get_spec(spec_id) or spec
    elif clone:
        run = st.store.get_run(clone)
        spec = run.spec if run else spec
    return render(request, "run_new.html", spec=spec, templates_list=st.store.list_specs(),
                  acknowledged=bool(st.db.get_setting("risk_acknowledged", False)))


@router.post("/plan", response_class=HTMLResponse)
async def plan(request: Request):
    st = state(request)
    form = await request.form()
    try:
        spec = spec_from_form(form)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<div class='text-red-700'>Invalid spec: {e}</div>")
    plan = st.pipeline.plan(spec)
    return render(request, "_plan.html", plan=plan, spec=spec)


@router.post("/new")
async def create_run(request: Request):
    st = state(request)
    form = await request.form()
    try:
        spec = spec_from_form(form)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<div class='text-red-700'>Invalid spec: {e}</div>", status_code=400)
    action = form.get("action") or "start"
    if form.get("acknowledge"):
        st.db.set_setting("risk_acknowledged", True)
    if action == "save":
        sid = st.store.save_spec(spec)
        return RedirectResponse(f"/runs/new?spec_id={sid}", status_code=303)
    if spec.run.source == "linkedin" and not st.db.get_setting("risk_acknowledged", False):
        return HTMLResponse("<div class='text-red-700'>Please acknowledge the LinkedIn account-risk notice first.</div>",
                            status_code=400)
    run = st.store.create_run(spec)
    await st.runner.start(run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/{run_id}")
async def run_detail(request: Request, run_id: str):
    st = state(request)
    run = st.store.get_run(run_id)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    return render(request, "run_detail.html", run=run, status_badge=status_badge,
                  companies=st.store.list_companies(run_id), counts=st.store.company_people_counts(run_id),
                  active=st.runner.is_active(run_id), history=st.events.history(run_id, limit=200))


@router.get("/{run_id}/stats", response_class=HTMLResponse)
async def run_stats(request: Request, run_id: str):
    st = state(request)
    run = st.store.get_run(run_id)
    if run is None:
        return HTMLResponse("", status_code=404)
    return render(request, "_run_stats.html", run=run, status_badge=status_badge, active=st.runner.is_active(run_id))


@router.get("/{run_id}/companies-table", response_class=HTMLResponse)
async def run_companies(request: Request, run_id: str):
    st = state(request)
    return render(request, "_run_companies.html", run_id=run_id, companies=st.store.list_companies(run_id),
                  counts=st.store.company_people_counts(run_id))


@router.post("/{run_id}/{action}")
async def run_action(request: Request, run_id: str, action: str):
    st = state(request)
    if action == "pause":
        msg = await st.runner.pause(run_id)
    elif action == "resume":
        msg = await st.runner.resume(run_id)
    elif action == "stop":
        msg = await st.runner.stop(run_id)
    elif action == "reclassify":
        from leadgen.pipeline.classify import reclassify_run

        msg = f"reclassified {reclassify_run(run_id, st.store)} people"
    elif action == "delete":
        if st.runner.is_active(run_id):
            msg = "stop the run first"
        else:
            st.store.delete_run(run_id)
            return RedirectResponse("/", status_code=303)
    else:
        msg = "unknown action"
    st.events.info(run_id, f"user action: {action} -> {msg}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/{run_id}/events")
async def run_events(request: Request, run_id: str, after: int = 0):
    st = state(request)

    async def gen():
        for ev in st.events.history(run_id, after_id=after, limit=500):
            yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
        current = st.store.get_run(run_id)
        if current is None or (current.status in ("done", "failed", "stopped") and not st.runner.is_active(run_id)):
            yield "event: done\ndata: {}\n\n"
            return
        q = st.events.subscribe(run_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=5)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    latest = st.store.get_run(run_id)
                    if latest and latest.status in ("done", "failed", "stopped") and not st.runner.is_active(run_id):
                        yield "event: done\ndata: {}\n\n"
                        break
                    continue
                if ev is None:
                    yield "event: done\ndata: {}\n\n"
                    break
                payload = {"ts": ev.ts.isoformat(timespec="seconds"), "level": ev.level, "msg": ev.msg, "data": ev.data}
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        finally:
            st.events.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
