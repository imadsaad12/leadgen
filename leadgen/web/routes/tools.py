"""Scraper tools: Company -> decision-makers (a focused background run) and Lead -> full profile (live)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from leadgen.scrape import decision_maker_spec, scrape_profile
from leadgen.sources.local import local_spec
from leadgen.sources.osm import CATEGORY_GROUPS, DEFAULT_CATEGORIES
from leadgen.web.app import render
from leadgen.web.routes.common import state

router = APIRouter(prefix="/tools")


@router.get("")
async def tools_page(request: Request, message: str = ""):
    st = state(request)
    return render(request, "tools.html", message=message,
                  categories=list(CATEGORY_GROUPS), default_categories=DEFAULT_CATEGORIES, countries=__import__("leadgen.pipeline.geo", fromlist=["country_names"]).country_names(),
                  acknowledged=bool(st.db.get_setting("risk_acknowledged", False)),
                  active=st.runner.active is not None and not st.runner.active.task.done())


@router.post("/company")
async def company_tool(request: Request):
    st = state(request)
    form = await request.form()
    company = str(form.get("company") or "").strip()
    if not company:
        return RedirectResponse("/tools?message=enter+a+company", status_code=303)
    if not st.db.get_setting("risk_acknowledged", False):
        return RedirectResponse("/tools?message=acknowledge+the+risk+notice+first", status_code=303)
    spec = decision_maker_spec(
        company,
        max_people=int(form.get("max_people") or 40),
        offer=str(form.get("offer") or "").strip(),
    )
    run = st.store.create_run(spec)
    await st.runner.start(run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/profile")
async def profile_tool(request: Request):
    st = state(request)
    form = await request.form()
    linkedin = str(form.get("linkedin") or "").strip()
    if not linkedin:
        return RedirectResponse("/tools?message=enter+a+profile+url+or+username", status_code=303)
    if st.runner.active is not None and not st.runner.active.task.done():
        return RedirectResponse("/tools?message=a+run+is+in+progress;+try+again+when+it+finishes", status_code=303)
    # make sure the sidecar is up (reuses the JobRunner's manager), then do a single live lookup
    if st.runner.manager is not None and not await st.runner.manager.healthy():
        try:
            await st.runner.manager.start()
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<pre class='p-4 text-red-700'>could not start the LinkedIn sidecar: {e}</pre>", status_code=502)
    from leadgen.sources.linkedin_mcp import LinkedInMCP, NotLoggedIn, unwrap_mcp_error
    from leadgen.store.cache import ToolCache

    sections = str(form.get("sections") or "experience,education,skills,certifications")
    try:
        async with LinkedInMCP(st.settings.mcp_url, cache=ToolCache(st.db), timeout_s=st.settings.mcp_tool_timeout_s) as mcp:
            data = await scrape_profile(mcp, linkedin, sections=sections)
    except BaseException as raw:  # noqa: BLE001 - MCP wraps errors in a task-group ExceptionGroup
        err = unwrap_mcp_error(raw)
        msg = str(err)
        if isinstance(err, NotLoggedIn):
            msg = "Your LinkedIn session has expired. Re-login from Settings (Import from browser or Log in manually), then retry."
        return render(request, "profile.html", error=msg, data=None)
    return render(request, "profile.html", error=None, data=data)


@router.post("/local")
async def local_tool(request: Request):
    st = state(request)
    form = await request.form()
    country = str(form.get("country") or "").strip()
    if not country:
        return RedirectResponse("/tools?message=choose+a+country", status_code=303)
    cats = [c for c in form.getlist("categories") if c] or list(DEFAULT_CATEGORIES)
    spec = local_spec(country, str(form.get("city") or "").strip() or None, cats,
                      max_results=int(form.get("max_results") or 120), offer=str(form.get("offer") or "").strip(),
                      backend=("gmaps" if str(form.get("backend")) == "gmaps" else "osm"))
    run = st.store.create_run(spec)
    await st.runner.start(run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)
