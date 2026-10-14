from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from leadgen.models import Department, Seniority
from leadgen.pipeline.classify import reclassify_run
from leadgen.titles.rules import classify_rule, normalize_key, save_override
from leadgen.web.app import render
from leadgen.web.routes.common import state

router = APIRouter(prefix="/titles")


@router.get("")
async def titles_page(request: Request, run_id: str = "", message: str = ""):
    st = state(request)
    unclassified = st.store.unclassified_titles(run_id or None, limit=300)
    for u in unclassified:
        u["suggestion"] = classify_rule(u["raw_title"])
    return render(request, "titles.html", unclassified=unclassified, run_id=run_id, message=message,
                  runs=st.store.list_runs(20), cache_stats=st.store.title_cache_stats(),
                  llm_on=bool(st.db.get_setting("llm_assist", True)))


@router.post("/assign")
async def assign(request: Request):
    st = state(request)
    form = await request.form()
    raw = str(form.get("raw_title") or "")
    dep = str(form.get("department") or "unclassified")
    sen = str(form.get("seniority") or "unknown")
    canonical = str(form.get("canonical") or "").strip() or None
    run_id = str(form.get("run_id") or "")
    base = classify_rule(raw)
    cls = base.model_copy(update={"department": Department(dep), "seniority": Seniority(sen),
                                  "canonical_title": canonical or base.canonical_title, "confidence": 1.0, "method": "manual"})
    key = base.normalized or normalize_key(raw)
    save_override(key, dep, sen, canonical)
    cls.normalized = key
    st.store.put_title(cls)
    for rid, pk in st.store.people_with_title(raw):
        st.store.set_classification(rid, pk, cls)
    return RedirectResponse(f"/titles?run_id={run_id}&message=saved+{len(st.store.people_with_title(raw))}+people", status_code=303)


@router.post("/llm")
async def classify_with_llm(request: Request):
    st = state(request)
    form = await request.form()
    run_id = str(form.get("run_id") or "")
    from leadgen.llm.claude_cli import build_claude_cli
    from leadgen.pipeline.classify import llm_classify_unclassified
    from leadgen.sources.base import RunContext
    from leadgen.store.cache import ToolCache

    cli = build_claude_cli(st.settings, st.store, st.events, run_id or None)
    if not cli.available():
        return RedirectResponse("/titles?message=claude+CLI+not+found", status_code=303)
    runs = [st.store.get_run(run_id)] if run_id else st.store.list_runs(20)
    updated = 0
    for run in runs:
        if run is None:
            continue
        ctx = RunContext(run_id=run.id, spec=run.spec, store=st.store, cache=ToolCache(st.db), events=st.events, llm=cli)
        updated += await llm_classify_unclassified(ctx)
    return RedirectResponse(f"/titles?run_id={run_id}&message=claude+updated+{updated}+people", status_code=303)


@router.post("/reclassify")
async def reclassify(request: Request):
    st = state(request)
    form = await request.form()
    run_id = str(form.get("run_id") or "")
    n = 0
    for run in ([st.store.get_run(run_id)] if run_id else st.store.list_runs(50)):
        if run:
            n += reclassify_run(run.id, st.store)
    return RedirectResponse(f"/titles?run_id={run_id}&message=reclassified+{n}+people", status_code=303)
