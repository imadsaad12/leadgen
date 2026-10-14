"""LinkedInSource: company discovery, profiling and employee enumeration through the MCP sidecar."""

from __future__ import annotations

from collections.abc import AsyncIterator

from leadgen.extract.common import company_slug
from leadgen.extract.companies import parse_company_about, parse_company_search, parse_job_companies
from leadgen.extract.people import parse_experience, parse_people_page
from leadgen.llm.schemas import PageExtraction
from leadgen.models import Company, Person, company_key_from_name
from leadgen.sources.base import RunContext
from leadgen.sources.linkedin_mcp import LinkedInMCP, RateLimited, ToolError
from leadgen.sources.queries import company_queries, lead_queries, people_facets

from leadgen.llm.prompts import EXTRACT_SYSTEM


class LinkedInSource:
    name = "linkedin"
    native_company_filters = frozenset({"keywords", "seed_companies"})
    native_people_filters = frozenset({"titles", "departments", "keywords", "locations", "network"})

    def __init__(self, mcp: LinkedInMCP) -> None:
        self.mcp = mcp

    # ------------------------------------------------------------------ discovery
    async def discover_companies(self, ctx: RunContext) -> AsyncIterator[Company]:
        cs = ctx.spec.company
        seen: set[str] = set()
        excluded = {company_slug(x) or x.strip().lower() for x in cs.exclude_companies}
        for seed in cs.seed_companies:
            slug = company_slug(seed) or seed.strip().strip("/").split("/")[-1].lower()
            if slug and slug not in seen and slug not in excluded:
                seen.add(slug)
                yield Company(key=slug, name=seed if not company_slug(seed) else slug.replace("-", " ").title(),
                              slug=slug, linkedin_url=f"https://www.linkedin.com/company/{slug}/", discovered_via=["seed"])
        queries = company_queries(ctx.spec)
        target = cs.max_companies * 3
        for q in queries:
            if len(seen) >= target:
                break
            for page in range(1, cs.max_search_pages_per_query + 1):
                ctx.check_cancelled()
                try:
                    payload = await self.mcp.call("search_companies", {"keywords": q, "page": page if page > 1 else None})
                except RateLimited:
                    raise
                except ToolError as e:
                    ctx.log(f"company search failed for {q!r}: {e.message}", "warning")
                    break
                parsed = parse_company_search(payload, found_via=f"search:{q}")
                if parsed.status != "ok":
                    ctx.log(f"company search page parsed as {parsed.status}: {'; '.join(parsed.notes)}", "warning", query=q)
                new = 0
                for c in parsed.companies:
                    if c.key in seen or c.key in excluded or c.name.lower() in excluded:
                        continue
                    seen.add(c.key)
                    new += 1
                    yield c
                    if len(seen) >= target:
                        break
                ctx.log(f"company search {q!r} page {page}: {len(parsed.companies)} cards, {new} new")
                if new == 0 or len(parsed.companies) < 5:
                    break
        if cs.discover_via_jobs:
            terms = cs.industries or cs.keywords
            for term in terms[:3]:
                for loc in cs.locations or [None]:
                    ctx.check_cancelled()
                    try:
                        payload = await self.mcp.call("search_jobs", {"keywords": term, "location": loc, "max_pages": 2})
                    except ToolError as e:
                        ctx.log(f"job search failed for {term!r}: {e.message}", "warning")
                        continue
                    new = 0
                    for c in parse_job_companies(payload):
                        if c.key in seen or c.key in excluded:
                            continue
                        seen.add(c.key)
                        new += 1
                        yield c
                    ctx.log(f"job search {term!r} {loc or ''}: {new} new companies")

    async def profile_company(self, company: Company, ctx: RunContext) -> Company:
        if not company.slug:
            return company
        payload = await self.mcp.call("get_company_profile", {"company_name": company.slug})
        profiled = parse_company_about(payload, company)
        if profiled.name == company.slug.replace("-", " ").title():
            # take the real name from the page's first line when the seed only gave a slug
            first = next((ln.strip() for ln in (payload.get("sections") or {}).get("about", "").splitlines() if ln.strip()), "")
            if first and len(first) < 80:
                profiled.name = first
        return profiled

    # ------------------------------------------------------------- people-first
    async def search_leads(self, ctx: RunContext):
        """People-first: find decision-makers in a location working in a domain; yield (Person, Company).

        The company is read from each person's headline ("Title at Company"). Location is enforced by the geo
        facet and double-checked against each person's own location string, so a wrong geo id cannot leak in.
        """
        from leadgen.pipeline.geo import canonical_country, location_country

        ps = ctx.spec.people
        seen: set[str] = set()
        cap = ctx.spec.company.max_companies * 50  # people-first has no per-company loop; cap total leads generously
        found = 0
        for q in lead_queries(ctx.spec):
            if found >= cap:
                break
            want = canonical_country(q.country) if q.country else None
            facet_id = q.id
            prog = ctx.store.facet(ctx.run_id, "_leads", facet_id)
            if prog and prog["status"] == "done":
                continue
            start_page = (prog["last_page"] + 1) if prog else 1
            for page in range(start_page, ps.max_pages_per_facet + 1):
                if found >= cap:
                    break
                ctx.check_cancelled()
                args = {"keywords": q.keywords, "geo_urn": q.geo_urn, "page": page if page > 1 else None}
                try:
                    payload = await self.mcp.call("search_people", args)
                except RateLimited:
                    raise
                except ToolError as e:
                    ctx.log(f"lead search {q.label!r} p{page} failed: {e.message}", "warning")
                    ctx.store.update_facet(ctx.run_id, "_leads", facet_id, last_page=page - 1, status="failed", new_found=0)
                    break
                parsed = parse_people_page(payload, company_key="", found_via=q.label)
                if parsed.status == "degraded" and ctx.llm is not None:
                    pass  # headline titles are usually fine; skip LLM here to save tokens
                new = 0
                off_country = 0
                for person in parsed.people:
                    if person.key in seen or (person.anonymized and not ps.include_anonymized):
                        continue
                    # location safety net: keep only people whose own location resolves to the target country
                    if want and person.location:
                        pc = location_country(person.location)
                        if pc and pc != want:
                            off_country += 1
                            continue
                    seen.add(person.key)
                    cname = person.company_name
                    person.company_key = company_key_from_name(cname) if cname else "name:unknown"
                    company = Company(key=person.company_key, name=cname or "(company unknown)",
                                      discovered_via=[f"lead:{q.label}"])
                    new += 1
                    found += 1
                    yield person, company
                    if found >= cap:
                        break
                ctx.store.update_facet(ctx.run_id, "_leads", facet_id, last_page=page, status="running", new_found=new)
                ctx.log(f"{q.label}: p{page} {len(parsed.people)} people, {new} new leads"
                        + (f", {off_country} dropped (wrong country)" if off_country else "") + f" (total {found})")
                if len(parsed.people) == 0 or (page > 1 and new == 0):
                    break
            ctx.store.update_facet(ctx.run_id, "_leads", facet_id, last_page=0, status="done", new_found=0)

    # ---------------------------------------------------------------- enumeration
    async def enumerate_people(self, company: Company, ctx: RunContext, known: set[str]) -> AsyncIterator[Person]:
        ps = ctx.spec.people
        cap = ps.max_per_company
        found = len(known)
        degraded_streak = 0

        async def handle(payload: dict, facet_label: str) -> list[Person]:
            nonlocal degraded_streak
            parsed = parse_people_page(payload, company_key=company.key, company_name=company.name, found_via=facet_label)
            people = parsed.people
            if parsed.function_breakdown and not company.function_breakdown:
                company.function_breakdown = parsed.function_breakdown
            if parsed.total_hint and not company.employee_count_hint and facet_label == "employees":
                company.employee_count_hint = parsed.total_hint
            if parsed.status == "degraded":
                degraded_streak += 1
                ctx.log(f"{company.name}: page parse degraded ({'; '.join(parsed.notes)})", "warning")
                if ctx.llm is not None:
                    people = await self._llm_extract(payload, people, company, facet_label, ctx) or people
            else:
                degraded_streak = 0
            if degraded_streak == 3:
                ctx.log("three consecutive degraded pages: LinkedIn's layout may have changed; record a fixture and "
                        "update leadgen/extract/people.py", "error")
            out: list[Person] = []
            for p in people:
                if p.key in known or (p.anonymized and not ps.include_anonymized):
                    continue
                known.add(p.key)
                out.append(p)
            return out

        # 1. the company's /people/ tab: seed people + function breakdown (one page load).
        # Skip it for a targeted decision-maker run (explicit titles, no broad sweep): it returns a generic
        # first page of employees that is mostly not who you want, and the URN already comes from the profile.
        targeted = bool(ps.titles) and not ps.broad_sweep
        want_people_tab = company.slug and (not targeted or not company.urn)
        if want_people_tab:
            facet_id = "employees"
            prog = ctx.store.facet(ctx.run_id, company.key, facet_id)
            if not prog or prog["status"] != "done":
                ctx.check_cancelled()
                try:
                    payload = await self.mcp.call("get_company_employees", {"company_name": company.slug})
                    new = await handle(payload, "employees")
                    for p in new:
                        yield p
                    found += len(new)
                    ctx.store.update_facet(ctx.run_id, company.key, facet_id, last_page=1, status="done", new_found=len(new))
                    ctx.log(f"{company.name}: people tab -> {len(new)} new people")
                except RateLimited:
                    raise
                except ToolError as e:
                    ctx.log(f"{company.name}: people tab failed: {e.message}", "warning")
                    ctx.store.update_facet(ctx.run_id, company.key, facet_id, last_page=0, status="failed", new_found=0)
        if found >= cap:
            return

        # 2. faceted, paged search_people (needs the numeric company URN)
        facets = people_facets(ctx.spec, company.function_breakdown)
        if not company.urn:
            ctx.log(f"{company.name}: no company URN; only keyword-filtered people-tab lookups are possible", "warning")
            facets = [f for f in facets if f.keywords][:6]
        empty_streak = 0
        for facet in facets:
            if found >= cap:
                break
            prog = ctx.store.facet(ctx.run_id, company.key, facet.id)
            if prog and prog["status"] == "done":
                continue
            start_page = (prog["last_page"] + 1) if prog else 1
            facet_new = 0
            for page in range(start_page, ps.max_pages_per_facet + 1):
                if found >= cap:
                    break
                ctx.check_cancelled()
                try:
                    if company.urn:
                        args = facet.args(company.urn)
                        args["page"] = page if page > 1 else None
                        payload = await self.mcp.call("search_people", args)
                    else:
                        if page > 1:
                            break
                        payload = await self.mcp.call("get_company_employees",
                                                      {"company_name": company.slug, "keywords": facet.keywords})
                except RateLimited:
                    raise
                except ToolError as e:
                    ctx.log(f"{company.name}: facet {facet.label!r} page {page} failed: {e.message}", "warning")
                    ctx.store.update_facet(ctx.run_id, company.key, facet.id, last_page=page - 1, status="failed", new_found=0)
                    break
                new = await handle(payload, facet.label)
                for p in new:
                    yield p
                found += len(new)
                facet_new += len(new)
                total_cards = len(parse_people_page(payload, company_key=company.key).people)
                ctx.store.update_facet(ctx.run_id, company.key, facet.id, last_page=page, status="running", new_found=len(new))
                ctx.log(f"{company.name}: {facet.label} p{page}: {total_cards} cards, {len(new)} new (total {found}/{cap})")
                if total_cards == 0 or (page > 1 and len(new) == 0) or total_cards < 5:
                    break
            ctx.store.update_facet(ctx.run_id, company.key, facet.id, last_page=0, status="done", new_found=0)
            empty_streak = empty_streak + 1 if facet_new == 0 else 0
            if empty_streak >= 4 and facet.keywords:
                ctx.log(f"{company.name}: four facets in a row added nobody; stopping targeted facets", "info")
                empty_streak = 0
                remaining_broad = [f for f in facets if not f.keywords]
                if not remaining_broad:
                    break

    async def _llm_extract(self, payload: dict, fallback: list[Person], company: Company, facet_label: str,
                           ctx: RunContext) -> list[Person] | None:
        from leadgen.extract.common import flatten_references, person_username
        from leadgen.llm.claude_cli import ClaudeCLIError

        allowed = []
        for ref in flatten_references(payload.get("references")):
            u = person_username(ref.get("url"))
            if u and f"/in/{u}/" not in allowed:
                allowed.append(f"/in/{u}/")
        if not allowed:
            return None
        text = "\n".join(v for v in (payload.get("sections") or {}).values() if isinstance(v, str))[:20000]
        prompt = f"Allowed profile paths (in page order):\n{chr(10).join(allowed)}\n\nPage text:\n{text}"
        try:
            result = await ctx.llm.structured(prompt, PageExtraction, system=EXTRACT_SYSTEM, kind="extract")
        except ClaudeCLIError as e:
            ctx.log(f"claude extraction failed: {e}", "warning")
            return None
        ctx.stats.llm_calls += 1
        by_path = {p.profile_path.strip(): p for p in result.people}
        out: list[Person] = []
        for path in allowed:
            u = person_username(path) or ""
            card = by_path.get(path)
            if not card:
                continue
            out.append(Person(
                key=u, username=u, profile_url=f"https://www.linkedin.com/in/{u}/", full_name=card.full_name,
                headline=None, raw_title=card.title, location=card.location, degree=card.degree,
                company_key=company.key, company_name=card.company or company.name, found_via=[facet_label],
                parse_method="llm", needs_review=not card.title,
            ))
        return out or None

    async def enrich_person(self, person: Person, ctx: RunContext) -> Person:
        payload = await self.mcp.call("get_person_profile",
                                      {"linkedin_username": person.username, "sections": "experience", "max_scrolls": 2})
        person.experience = parse_experience(payload)
        if person.experience and not person.raw_title:
            person.raw_title = person.experience[0].title
            person.needs_review = False
        return person

    async def close(self) -> None:
        try:
            await self.mcp.call("close_session", {}, use_cache=False)
        except Exception:
            pass
