"""Pipeline orchestrator: discover -> profile/filter -> enumerate -> classify -> export, with checkpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leadgen.config import Settings
from leadgen.export.report import write_all_exports
from leadgen.models import Company, RunState, Spec
from leadgen.pipeline.classify import classify_title, excluded_by_spec, llm_classify_unclassified
from leadgen.pipeline.filter import post_filter
from leadgen.sources.base import RunCancelled, RunContext, SourceAdapter
from leadgen.sources.queries import estimate_page_loads
from leadgen.store.budget import Budget, BudgetExhausted, CircuitOpen
from leadgen.store.cache import ToolCache
from leadgen.store.db import Database
from leadgen.store.events import EventBus
from leadgen.store.runs import RunStore
from leadgen.titles.rules import load_overrides


class Pipeline:
    def __init__(self, settings: Settings, db: Database, store: RunStore, events: EventBus) -> None:
        self.settings = settings
        self.db = db
        self.store = store
        self.events = events

    # ----------------------------------------------------------------- planning
    def plan(self, spec: Spec) -> dict[str, Any]:
        from leadgen.sources.queries import company_queries, lead_queries, people_facets

        if spec.run.strategy == "people_first":
            lq = lead_queries(spec)
            per = spec.people.max_pages_per_facet
            return {
                "estimate": {"company_queries": 0, "discovery_pages": 0, "profile_pages": 0,
                             "companies": spec.company.max_companies, "facets_per_company": 0,
                             "people_pages": len(lq) * per, "total": len(lq) * per},
                "strategy": "people_first",
                "lead_queries": [{"keywords": q.keywords, "country": q.country, "geo_urn": q.geo_urn} for q in lq],
                "capabilities": spec.capability_matrix(),
            }
        est = estimate_page_loads(spec)
        return {
            "estimate": est,
            "strategy": "company_first",
            "company_queries": company_queries(spec),
            "facets": [{"label": f.label, "keywords": f.keywords, "location": f.location} for f in people_facets(spec)],
            "capabilities": spec.capability_matrix(),
        }

    # ------------------------------------------------------------------ running
    async def execute(self, run: RunState, source_factory, *, pause: asyncio.Event, stop: asyncio.Event,
                      llm=None) -> RunState:
        """source_factory(ctx) -> async context manager yielding a SourceAdapter."""
        spec = run.spec
        cache = ToolCache(self.db)
        budget = Budget(self.db, spec.run)
        ctx = RunContext(run_id=run.id, spec=spec, store=self.store, cache=cache, events=self.events, budget=budget,
                         llm=llm, stats=run.stats, pause_requested=pause, stop_requested=stop)
        run.status = "running"
        run.started_at = run.started_at or datetime.now(UTC)
        run.message = None
        self.store.save_run(run)
        ctx.log(f"run started: {spec.name} (source={spec.run.source})", budget=budget.summary())
        try:
            async with source_factory(ctx) as source:
                if spec.run.strategy == "people_first":
                    await self._run_people_first(ctx, source)
                else:
                    await self._discover(ctx, source)
                    await self._profile_and_filter(ctx, source)
                    await self._enumerate(ctx, source)
                    await self._enrich(ctx, source)
            await self._classify_with_llm(ctx)
            await self._filter_relevance(ctx)
            run.status = "done"
            run.stats = ctx.stats
            run.stats.page_loads = budget.session_page_loads
            run.stats.cache_hits = cache.hits
            self.store.save_run(run)
            out_dir = self._export(ctx)
            run.message = f"exports written to {out_dir}"
        except RunCancelled as e:
            run.status = e.status
            run.message = f"{e.status} by user"
            ctx.log(f"run {e.status}")
        except (BudgetExhausted, CircuitOpen) as e:
            run.status = "paused"
            run.message = str(e)
            ctx.log(str(e), "warning")
        except Exception as e:  # noqa: BLE001 - surface anything else as a failed run
            from leadgen.sources.linkedin_mcp import NotLoggedIn, RateLimited, unwrap_mcp_error

            err = unwrap_mcp_error(e)
            if isinstance(err, NotLoggedIn):
                run.status = "paused"
                run.message = "LinkedIn session expired; re-login from Settings and resume."
            elif isinstance(err, RateLimited):
                run.status = "paused"
                run.message = f"LinkedIn rate-limited the run; resume later. ({err})"
            else:
                run.status = "failed"
                run.message = f"{type(err).__name__}: {err}"
            ctx.log(run.message, "error" if run.status == "failed" else "warning")
        finally:
            run.stats = ctx.stats
            run.stats.cache_hits = cache.hits
            run.stats.page_loads = budget.session_page_loads
            if run.status in ("done", "failed", "stopped"):
                run.finished_at = datetime.now(UTC)
            self.store.save_run(run)
            self.events.close_run(run.id)
        return run

    async def _discover(self, ctx: RunContext, source: SourceAdapter) -> None:
        existing = self.store.list_companies(ctx.run_id)
        if existing:
            ctx.log(f"resume: {len(existing)} companies already known, skipping discovery")
            ctx.stats.companies_discovered = len(existing)
            return
        n = 0
        async for company in source.discover_companies(ctx):
            self.store.upsert_company(ctx.run_id, company)
            n += 1
            ctx.stats.companies_discovered = n
            if n % 10 == 0:
                self.store.save_stats(ctx.run_id, ctx.stats)
        ctx.log(f"discovery finished: {n} candidate companies")
        if n == 0:
            ctx.log("no companies found; add seed_companies or broaden industries/keywords", "warning")

    async def _run_people_first(self, ctx: RunContext, source) -> None:
        """Find decision-makers by (role x domain x country) people search, deriving companies from headlines."""
        if not hasattr(source, "search_leads"):
            raise RuntimeError(f"source {getattr(source, 'name', '?')!r} does not support people-first discovery")
        overrides = load_overrides()
        companies: dict[str, Company] = {}
        known = self.store.people_keys(ctx.run_id)
        async for person, company in source.search_leads(ctx):
            if person.key in known:
                continue
            known.add(person.key)
            if excluded_by_spec(person, ctx.spec):
                continue
            cls = classify_title(person.raw_title, self.store, overrides)
            self.store.upsert_person(ctx.run_id, person, cls)
            ctx.stats.people_found += 1
            if cls.department.value == "unclassified":
                ctx.stats.people_unclassified += 1
            else:
                ctx.stats.people_classified += 1
            c = companies.get(company.key)
            if c is None:
                c = company
                c.status = "enumerated"
                companies[company.key] = c
            c.people_found += 1
            # Persist the company immediately so the run page's table fills in live as leads arrive,
            # and keep the company-count stats current on every new company.
            self.store.upsert_company(ctx.run_id, c)
            ctx.stats.companies_discovered = len(companies)
            ctx.stats.companies_passed = len(companies)
            ctx.stats.companies_enumerated = len(companies)
            if ctx.stats.people_found % 5 == 0:
                ctx.stats.page_loads = ctx.budget.session_page_loads if ctx.budget else ctx.stats.page_loads
                self.store.save_stats(ctx.run_id, ctx.stats)
        ctx.stats.page_loads = ctx.budget.session_page_loads if ctx.budget else ctx.stats.page_loads
        self.store.save_stats(ctx.run_id, ctx.stats)
        ctx.log(f"people-first done: {ctx.stats.people_found} leads across {len(companies)} companies")

    async def _profile_and_filter(self, ctx: RunContext, source: SourceAdapter) -> None:
        from leadgen.pipeline.geo import canonical_country, location_country

        cs = ctx.spec.company
        wanted_countries = {c for c in (canonical_country(loc) for loc in cs.locations) if c}
        passed = len(self.store.list_companies(ctx.run_id, "profiled")) + \
            len(self.store.list_companies(ctx.run_id, "enumerating")) + len(self.store.list_companies(ctx.run_id, "enumerated"))
        skipped_by_card = 0
        for company in self.store.list_companies(ctx.run_id, "discovered"):
            if passed >= cs.max_companies:
                break
            ctx.check_cancelled()
            # Cheap pre-filter: the search-result card already shows the HQ city. If that resolves to a
            # country you did NOT ask for, reject here and never spend a page load on its About page.
            # Unresolvable card locations fall through to the normal profile + filter (never guess).
            if wanted_countries and company.hq:
                card_country = location_country(company.hq)
                if card_country and card_country not in wanted_countries:
                    company.status = "filtered_out"
                    company.filter_reasons = [f"card HQ {company.hq!r} is in {card_country}, not {sorted(wanted_countries)}"]
                    self.store.upsert_company(ctx.run_id, company)
                    skipped_by_card += 1
                    continue
            try:
                profiled = await source.profile_company(company, ctx)
            except Exception as e:  # RateLimited/BudgetExhausted propagate; other tool errors mark the company failed
                from leadgen.sources.linkedin_mcp import RateLimited, ToolError
                if isinstance(e, RateLimited) or not isinstance(e, ToolError):
                    raise
                company.status = "failed"
                company.filter_reasons = [f"profile failed: {e.message}"]
                self.store.upsert_company(ctx.run_id, company)
                ctx.log(f"{company.name}: profile failed ({e.message})", "warning")
                continue
            ok, reasons, unknown = post_filter(profiled, ctx.spec)
            profiled.unknown_fields = sorted(set(profiled.unknown_fields) | set(unknown))
            profiled.filter_reasons = reasons
            profiled.status = "profiled" if ok else "filtered_out"
            self.store.upsert_company(ctx.run_id, profiled)
            if ok:
                passed += 1
                ctx.stats.companies_passed = passed
            self.store.save_stats(ctx.run_id, ctx.stats)
            ctx.log(f"{profiled.name}: {'passed' if ok else 'filtered out'}"
                    + (f" ({'; '.join(reasons)})" if reasons else "")
                    + (f" [unknown: {', '.join(unknown)}]" if unknown else ""),
                    industry=profiled.industry, hq=profiled.hq, size=profiled.size_bucket)
        if skipped_by_card:
            ctx.log(f"skipped {skipped_by_card} companies on the search card's location (no About-page load spent)")
        ctx.log(f"{passed} companies pass the filters")

    async def _enumerate(self, ctx: RunContext, source: SourceAdapter) -> None:
        overrides = load_overrides()
        todo = self.store.list_companies(ctx.run_id, "profiled") + self.store.list_companies(ctx.run_id, "enumerating")
        for company in todo:
            ctx.check_cancelled()
            company.status = "enumerating"
            self.store.upsert_company(ctx.run_id, company)
            known = self.store.people_keys(ctx.run_id, company.key)
            before = len(known)
            async for person in source.enumerate_people(company, ctx, known):
                if excluded_by_spec(person, ctx.spec):
                    continue
                cls = classify_title(person.raw_title, self.store, overrides)
                self.store.upsert_person(ctx.run_id, person, cls)
                ctx.stats.people_found += 1
                if cls.department.value == "unclassified":
                    ctx.stats.people_unclassified += 1
                else:
                    ctx.stats.people_classified += 1
                if ctx.stats.people_found % 10 == 0:
                    ctx.stats.page_loads = ctx.budget.session_page_loads if ctx.budget else ctx.stats.page_loads
                    self.store.save_stats(ctx.run_id, ctx.stats)
            company.people_found = len(self.store.people_keys(ctx.run_id, company.key))
            company.status = "enumerated"
            self.store.upsert_company(ctx.run_id, company)
            ctx.stats.companies_enumerated += 1
            ctx.stats.page_loads = ctx.budget.session_page_loads if ctx.budget else ctx.stats.page_loads
            self.store.save_stats(ctx.run_id, ctx.stats)
            ctx.log(f"{company.name}: enumeration done, {company.people_found} people (+{company.people_found - before})")

    async def _enrich(self, ctx: RunContext, source: SourceAdapter) -> None:
        es = ctx.spec.people.enrich
        if not es.enabled or es.max_per_company <= 0:
            return
        from leadgen.pipeline.classify import matches_spec

        for company in self.store.list_companies(ctx.run_id, "enumerated"):
            n = 0
            for person, cls in self.store.list_people(ctx.run_id, company_key=company.key, limit=1000):
                if n >= es.max_per_company:
                    break
                if person.experience is not None or not matches_spec(cls, ctx.spec):
                    continue
                ctx.check_cancelled()
                try:
                    enriched = await source.enrich_person(person, ctx)
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"enrich {person.username} failed: {e}", "warning")
                    continue
                self.store.upsert_person(ctx.run_id, enriched, classify_title(enriched.raw_title, self.store))
                n += 1

    async def _classify_with_llm(self, ctx: RunContext) -> None:
        if ctx.llm is None:
            return
        n = await llm_classify_unclassified(ctx)
        if n:
            ctx.stats.people_classified += n
            ctx.stats.people_unclassified = max(0, ctx.stats.people_unclassified - n)

    async def _filter_relevance(self, ctx: RunContext) -> None:
        """Final step: Claude scores each lead against the offer and excludes poor fits (when an offer is set)."""
        from leadgen.pipeline.relevance import filter_relevance

        offer = (ctx.spec.offer or "").strip()
        if not offer:
            return
        if ctx.llm is None:
            ctx.log("offer set but Claude CLI unavailable; skipping the relevance filter", "warning")
            return
        ctx.log("scoring lead relevance against the offer with Claude...")
        counts = await filter_relevance(ctx)
        if counts:
            ctx.log(f"relevance filter done: {counts.get('relevant', 0)} kept, {counts.get('excluded', 0)} excluded")

    def _export(self, ctx: RunContext) -> Path:
        out_dir = self.settings.outputs / ctx.run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        write_all_exports(ctx.run_id, self.store, out_dir, stats=ctx.stats, budget=ctx.budget.summary() if ctx.budget else None)
        ctx.log(f"exports written to {out_dir}")
        return out_dir
