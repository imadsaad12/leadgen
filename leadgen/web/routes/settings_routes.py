from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from leadgen.web.app import render
from leadgen.web.routes.common import state

router = APIRouter(prefix="/settings")

DEFAULTS = {"daily_budget": 100, "monthly_search_cap": 250, "min_delay_s": 8.0, "max_delay_s": 20.0,
            "llm_assist": True, "autostart_sidecar": False, "retention_days": 90}


@router.get("")
async def settings_page(request: Request, message: str = ""):
    st = state(request)
    values = {k: st.db.get_setting(k, v) for k, v in DEFAULTS.items()}
    login_running = st.login_task is not None and not st.login_task.done()
    return render(request, "settings.html", values=values, message=message, sidecar=st.manager.running(),
                  healthy=(await st.manager.healthy()) if st.manager.running() else False,
                  server_log=st.manager.log_lines[-40:], login_running=login_running, login_result=st.login_result,
                  acknowledged=bool(st.db.get_setting("risk_acknowledged", False)))


@router.post("")
async def save_settings(request: Request):
    st = state(request)
    form = await request.form()
    for key, default in DEFAULTS.items():
        if isinstance(default, bool):
            st.db.set_setting(key, bool(form.get(key)))
        elif isinstance(default, float):
            st.db.set_setting(key, float(form.get(key) or default))
        else:
            st.db.set_setting(key, int(form.get(key) or default))
    if form.get("acknowledge"):
        st.db.set_setting("risk_acknowledged", True)
    return RedirectResponse("/settings?message=saved", status_code=303)


@router.post("/server/{action}")
async def server_action(request: Request, action: str):
    st = state(request)
    if action == "start":
        try:
            msg = await st.manager.start()
        except Exception as e:  # noqa: BLE001
            msg = f"start failed: {e}"
    elif action == "stop":
        msg = await st.manager.stop()
    elif action == "logout":
        code, out = await st.manager.logout()
        msg = f"logout exit {code}"
    else:
        msg = "unknown action"
    return RedirectResponse(f"/settings?message={msg}", status_code=303)


@router.post("/login/{mode}")
async def login(request: Request, mode: str):
    st = state(request)
    if st.login_task is not None and not st.login_task.done():
        return RedirectResponse("/settings?message=login+already+in+progress", status_code=303)

    async def run_login():
        if mode == "manual":
            st.login_result = await st.manager.manual_login()
        else:
            form = await request.form()
            browser = str(form.get("browser") or "chrome")
            st.login_result = await st.manager.import_from_browser(browser)

    st.login_result = None
    st.login_task = asyncio.create_task(run_login())
    return RedirectResponse("/settings?message=login+started", status_code=303)


@router.get("/doctor", response_class=HTMLResponse)
async def doctor(request: Request):
    st = state(request)
    checks = await st.manager.doctor(live=True)
    return render(request, "_doctor.html", checks=checks)


@router.post("/purge")
async def purge(request: Request):
    st = state(request)
    form = await request.form()
    days = int(form.get("days") or st.db.get_setting("retention_days", 90))
    result = st.store.purge_older_than(days)
    return RedirectResponse(f"/settings?message=purged+{result}", status_code=303)


@router.post("/forget")
async def forget(request: Request):
    st = state(request)
    form = await request.form()
    from leadgen.extract.common import person_username

    profile = str(form.get("profile") or "")
    username = person_username(profile) or profile.strip().strip("/").split("/")[-1]
    n = st.store.forget_person(username) if username else 0
    return RedirectResponse(f"/settings?message=removed+{n}+records+for+{username}", status_code=303)
