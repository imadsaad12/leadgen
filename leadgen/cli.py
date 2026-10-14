"""Command-line entry points: serve the web app, doctor, login, run a spec, reclassify, export, status, purge."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from leadgen.config import Settings, get_settings
from leadgen.models import Spec

app = typer.Typer(help="LeadGen: LinkedIn lead generation (local web app + CLI).", no_args_is_help=True)
console = Console()


def _settings() -> Settings:
    s = get_settings()
    s.ensure_dirs()
    return s


def _stores(settings: Settings):
    from leadgen.store.db import Database
    from leadgen.store.events import EventBus
    from leadgen.store.runs import RunStore

    db = Database(settings.database)
    return db, RunStore(db), EventBus(db)


def load_spec(path: Path) -> Spec:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "name" not in data:
        data["name"] = path.stem
    return Spec.model_validate(data)


@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None, reload: bool = False) -> None:
    """Start the local web app (default http://127.0.0.1:8080)."""
    import uvicorn

    s = _settings()
    uvicorn.run("leadgen.web.app:create_app", factory=True, host=host or s.host, port=port or s.port, reload=reload)


@app.command()
def doctor(live: bool = typer.Option(True, help="Also probe the running sidecar and the claude CLI")) -> None:
    """Check the toolchain, the LinkedIn sidecar, the session and the claude CLI."""
    from leadgen.web.server_manager import ServerManager

    s = _settings()
    db, store, events = _stores(s)
    mgr = ServerManager(s, db)
    report = asyncio.run(mgr.doctor(live=live))
    table = Table(title="leadgen doctor")
    table.add_column("check")
    table.add_column("ok")
    table.add_column("detail")
    for item in report:
        table.add_row(item["check"], "✅" if item["ok"] else "❌", str(item.get("detail", "")))
    console.print(table)


@app.command()
def login(browser: Optional[str] = typer.Option(None, help="Import the session from this browser (chrome, brave, edge, arc, vivaldi) instead of logging in manually")) -> None:
    """Sign in to LinkedIn for the sidecar (opens a browser window) or import an existing browser session."""
    from leadgen.web.server_manager import ServerManager

    s = _settings()
    db, _, _ = _stores(s)
    mgr = ServerManager(s, db)
    code, output = asyncio.run(mgr.import_from_browser(browser) if browser else mgr.manual_login())
    console.print(output)
    raise typer.Exit(code)


@app.command()
def run(spec: Path, source: Optional[str] = None, max_companies: Optional[int] = None,
        max_people_per_company: Optional[int] = None, budget: Optional[int] = None,
        resume: Optional[str] = typer.Option(None, help="Resume an existing run id"),
        dry_run: bool = False, no_llm: bool = False) -> None:
    """Run a spec file end to end and write exports."""
    from leadgen.pipeline.run import Pipeline
    from leadgen.sources import make_source_factory

    s = _settings()
    db, store, events = _stores(s)
    pipeline = Pipeline(s, db, store, events)
    if resume:
        state = store.get_run(resume)
        if state is None:
            console.print(f"[red]run {resume} not found")
            raise typer.Exit(2)
        spec_obj = state.spec
    else:
        spec_obj = load_spec(spec)
    if source:
        spec_obj.run.source = source
    if max_companies:
        spec_obj.company.max_companies = max_companies
    if max_people_per_company:
        spec_obj.people.max_per_company = max_people_per_company
    if budget:
        spec_obj.run.daily_budget = budget
    if dry_run:
        console.print_json(json.dumps(pipeline.plan(spec_obj), default=str))
        return
    state = state if resume else store.create_run(spec_obj)
    state.spec = spec_obj
    llm = None
    if not no_llm and spec_obj.run.llm_assist is not False:
        from leadgen.llm.claude_cli import build_claude_cli

        cli = build_claude_cli(s, store, events, state.id)
        llm = cli if cli.available() else None

    async def _go():
        q = events.subscribe(state.id)
        events.bind_loop(asyncio.get_running_loop())

        async def printer():
            while True:
                ev = await q.get()
                if ev is None:
                    break
                color = {"warning": "yellow", "error": "red", "debug": "dim"}.get(ev.level, "white")
                console.print(f"[{color}]{ev.ts:%H:%M:%S} {ev.msg}")

        task = asyncio.create_task(printer())
        try:
            return await pipeline.execute(state, make_source_factory(spec_obj.run.source, s),
                                          pause=asyncio.Event(), stop=asyncio.Event(), llm=llm)
        finally:
            events.close_run(state.id)
            await task

    result = asyncio.run(_go())
    console.print(f"[bold]run {result.id}: {result.status}[/bold] {result.message or ''}")
    console.print(result.stats.model_dump())


@app.command()
def company(company: str, max_people: int = 40,
            offer: str = typer.Option("", help="Optional offer text to run the relevance filter"),
            source: Optional[str] = None, no_llm: bool = False) -> None:
    """Scrape ONE company and list its decision-makers (director / VP / C-level / founder)."""
    from leadgen.pipeline.run import Pipeline
    from leadgen.scrape import decision_maker_spec
    from leadgen.sources import make_source_factory

    s = _settings()
    db, store, events = _stores(s)
    spec = decision_maker_spec(company, max_people=max_people, offer=offer, source=source or "linkedin")
    pipeline = Pipeline(s, db, store, events)
    state = store.create_run(spec)
    llm = None
    if offer and not no_llm and spec.run.llm_assist is not False:
        from leadgen.llm.claude_cli import build_claude_cli
        cli = build_claude_cli(s, store, events, state.id)
        llm = cli if cli.available() else None

    async def _go():
        q = events.subscribe(state.id); events.bind_loop(asyncio.get_running_loop())
        async def printer():
            while True:
                ev = await q.get()
                if ev is None: break
                if ev.level != "debug": console.print(f"[dim]{ev.ts:%H:%M:%S}[/dim] {ev.msg}")
        t = asyncio.create_task(printer())
        try:
            return await pipeline.execute(state, make_source_factory(spec.run.source, s),
                                          pause=asyncio.Event(), stop=asyncio.Event(), llm=llm)
        finally:
            events.close_run(state.id); await t

    result = asyncio.run(_go())
    from leadgen.pipeline.classify import matches_spec
    rows = store.list_people(result.id, limit=1000)
    dm = [(p, c) for p, c in rows if matches_spec(c, spec)]
    console.print(f"\n[bold]{result.spec.name}: {len(dm)} decision-makers[/bold] (of {len(rows)} people)")
    table = Table()
    for col in ("name", "title", "department", "level", "profile"):
        table.add_column(col)
    for person, c in sorted(dm, key=lambda x: (x[1].department.value if x[1] else "z")):
        table.add_row(person.full_name, (person.raw_title or "")[:40],
                      c.department.value if c else "", c.seniority.value if c else "", person.profile_url)
    console.print(table)
    console.print(f"[dim]exports in {s.outputs / result.id}[/dim]")


@app.command()
def profile(linkedin: str, sections: str = "experience,education,skills,certifications",
            out: Optional[Path] = None) -> None:
    """Scrape ONE person's full LinkedIn profile."""
    from leadgen.scrape import scrape_profile
    from leadgen.sources.linkedin_mcp import LinkedInMCP
    from leadgen.store.cache import ToolCache

    s = _settings()
    db, store, events = _stores(s)

    async def _go():
        async with LinkedInMCP(s.mcp_url, cache=ToolCache(db), timeout_s=s.mcp_tool_timeout_s) as mcp:
            return await scrape_profile(mcp, linkedin, sections=sections)

    data = asyncio.run(_go())
    console.print(f"[bold]{data['name']}[/bold]  {data.get('headline') or ''}")
    if data.get("location"):
        console.print(f"[dim]{data['location']}[/dim]")
    console.print(data["url"])
    c = data.get("contacts", {})
    if c.get("available"):
        if c.get("emails"): console.print("[bold]Email[/bold]: " + ", ".join(c["emails"]))
        if c.get("phones"): console.print("[bold]Phone[/bold]: " + ", ".join(c["phones"]))
        if c.get("websites"): console.print("[bold]Websites[/bold]: " + ", ".join(c["websites"]))
    else:
        console.print("[dim]No email/phone shared (visible mainly for 1st-degree connections).[/dim]")
    if data["experience"]:
        console.print("\n[bold]Experience[/bold]")
        for e in data["experience"]:
            console.print(f"  - {e.get('title') or ''} @ {e.get('company') or ''} ({e.get('dates') or ''})")
    import json as _json
    target = out or (s.outputs / f"profile_{data['username']}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(data, ensure_ascii=False, indent=2))
    console.print(f"[dim]saved {target}[/dim]")


@app.command()
def reclassify(run_id: str) -> None:
    """Re-apply overrides, title cache and rules to a run (no LinkedIn calls)."""
    from leadgen.pipeline.classify import reclassify_run

    s = _settings()
    db, store, _ = _stores(s)
    n = reclassify_run(run_id, store)
    console.print(f"reclassified {n} people")


@app.command()
def export(run_id: str, out: Optional[Path] = None) -> None:
    """Write CSV / JSON / XLSX / report for a run."""
    from leadgen.export.report import write_all_exports

    s = _settings()
    db, store, _ = _stores(s)
    state = store.get_run(run_id)
    if state is None:
        console.print(f"[red]run {run_id} not found")
        raise typer.Exit(2)
    paths = write_all_exports(run_id, store, out or (s.outputs / run_id), stats=state.stats)
    for k, p in paths.items():
        console.print(f"{k}: {p}")


@app.command()
def status() -> None:
    """Show runs, budget usage and cache size."""
    from leadgen.store.budget import Budget
    from leadgen.store.cache import ToolCache

    s = _settings()
    db, store, _ = _stores(s)
    table = Table(title="runs")
    for col in ("id", "name", "status", "companies", "people", "page loads", "message"):
        table.add_column(col)
    for r in store.list_runs(20):
        table.add_row(r.id, r.spec.name, r.status, str(r.stats.companies_enumerated), str(r.stats.people_found),
                      str(r.stats.page_loads), (r.message or "")[:60])
    console.print(table)
    from leadgen.models import RunSpec

    b = Budget(db, RunSpec())
    console.print("budget:", b.summary())
    console.print("cache:", ToolCache(db).stats())
    console.print("title cache:", store.title_cache_stats())


@app.command()
def purge(older_than_days: int = 90, cache_only: bool = False) -> None:
    """Delete runs and cached pages older than N days (data retention)."""
    from leadgen.store.cache import ToolCache

    s = _settings()
    db, store, _ = _stores(s)
    if cache_only:
        n = ToolCache(db).purge(older_than=timedelta(days=older_than_days))
        console.print(f"purged {n} cache entries")
    else:
        console.print(store.purge_older_than(older_than_days))


@app.command()
def forget(profile: str) -> None:
    """Erase one person (LinkedIn username or URL) from all runs and the cache."""
    from leadgen.extract.common import person_username

    s = _settings()
    db, store, _ = _stores(s)
    username = person_username(profile) or profile.strip().strip("/").split("/")[-1]
    console.print(f"removed {store.forget_person(username)} records for {username}")


if __name__ == "__main__":
    app()
