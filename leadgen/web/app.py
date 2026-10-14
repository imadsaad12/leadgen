"""FastAPI application factory for the local LeadGen web app."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from leadgen.config import get_settings
from leadgen.models import SENIORITY_ORDER, SIZE_BUCKETS, Department, Seniority
from leadgen.pipeline.geo import country_names
from leadgen.pipeline.run import Pipeline
from leadgen.store.db import Database
from leadgen.store.events import EventBus
from leadgen.store.runs import RunStore
from leadgen.titles.rules import department_label
from leadgen.web.jobs import JobRunner
from leadgen.web.server_manager import ServerManager

WEB_DIR = Path(__file__).resolve().parent
log = logging.getLogger("leadgen.web")


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings.database)
    store = RunStore(db)
    events = EventBus(db)
    pipeline = Pipeline(settings, db, store, events)
    manager = ServerManager(settings, db)
    runner = JobRunner(settings, pipeline, store, events, manager=manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        events.bind_loop(asyncio.get_running_loop())
        recovered = runner.recover()
        if recovered:
            log.info("marked %d interrupted run(s) as paused", recovered)
        if db.get_setting("autostart_sidecar", False):
            try:
                await manager.start()
            except Exception as e:  # noqa: BLE001
                log.warning("sidecar autostart failed: %s", e)
        yield
        await runner.shutdown()
        await manager.stop()
        db.close()

    app = FastAPI(title="LeadGen", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.store = store
    app.state.events = events
    app.state.pipeline = pipeline
    app.state.runner = runner
    app.state.manager = manager
    app.state.login_task = None
    app.state.login_result = None

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.globals.update(
        departments=[d for d in Department if d != Department.UNCLASSIFIED],
        department_label=department_label,
        seniorities=[s for s in SENIORITY_ORDER if s != Seniority.UNKNOWN],
        size_buckets=SIZE_BUCKETS,
        countries=country_names(),
        asset_ver=int((WEB_DIR / "static" / "app.css").stat().st_mtime),
    )
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from leadgen.web.routes import all_routers

    for router in all_routers():
        app.include_router(router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("unhandled error on %s", request.url.path)
        return HTMLResponse(f"<pre class='p-4 text-red-600'>{type(exc).__name__}: {exc}</pre>", status_code=500)

    return app


def render(request: Request, name: str, **ctx):
    templates: Jinja2Templates = request.app.state.templates
    ctx.setdefault("settings", request.app.state.settings)
    ctx.setdefault("request", request)
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, name, ctx)
