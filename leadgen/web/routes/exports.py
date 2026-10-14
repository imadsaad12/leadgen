from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from leadgen.export.report import write_all_exports
from leadgen.web.routes.common import state

router = APIRouter(prefix="/runs/{run_id}")

FILES = {"csv": "leads.csv", "companies": "companies.csv", "json": "run.json", "jsonl": "leads.jsonl",
         "xlsx": "leads.xlsx", "report": "report.md"}


@router.get("/export")
async def export(request: Request, run_id: str, format: str = "xlsx"):
    st = state(request)
    run = st.store.get_run(run_id)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    out_dir = st.settings.outputs / run_id
    write_all_exports(run_id, st.store, out_dir, stats=run.stats)
    name = FILES.get(format, "leads.xlsx")
    path = out_dir / name
    media = {"csv": "text/csv", "md": "text/markdown", "json": "application/json", "jsonl": "application/x-ndjson",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[path.suffix.lstrip(".")]
    return FileResponse(path, media_type=media, filename=f"{run.spec.name}-{name}")
