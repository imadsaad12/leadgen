"""Local-business lead source (OpenStreetMap). Yields businesses with phone/website as leads.

Plugs into the people-first pipeline: each business becomes a lead (the business is the contact), so the live
table, the Claude relevance filter and the exports all work unchanged. No LinkedIn, no login, no ban risk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from leadgen.models import (
    Company,
    CompanySpec,
    PeopleSpec,
    Person,
    RunSpec,
    Spec,
    company_key_from_name,
)
from leadgen.sources.base import RunContext
from leadgen.sources.osm import DEFAULT_CATEGORIES, OSMClient


def local_spec(country: str, city: str | None, categories: list[str], *, max_results: int = 120,
               offer: str = "", backend: str = "osm") -> Spec:
    """A local-business run for one country/city and a set of category groups.

    backend: "osm" (OpenStreetMap, free, instant, thinner) or "gmaps" (self-hosted Google Maps scraper, richer).
    """
    label = ", ".join(x for x in (city, country) if x) or "local"
    src = "Google Maps" if backend == "gmaps" else "OpenStreetMap"
    return Spec(
        name=f"local businesses ({src}): {label}",
        offer=offer,
        company=CompanySpec(locations=[country], city=city or None,
                            categories=categories or list(DEFAULT_CATEGORIES),
                            local_backend=backend,
                            max_companies=max(1, min(max_results, 500))),
        people=PeopleSpec(),
        run=RunSpec(source="local", strategy="people_first"),
    )


class LocalSource:
    name = "local"
    native_company_filters = frozenset({"locations", "city", "categories"})
    native_people_filters = frozenset()

    def __init__(self, client: OSMClient | None = None, gmaps=None) -> None:
        self.client = client or OSMClient()
        self.gmaps = gmaps

    async def search_leads(self, ctx: RunContext) -> AsyncIterator[tuple[Person, Company]]:
        import asyncio

        cs = ctx.spec.company
        country = cs.locations[0] if cs.locations else "Lebanon"
        city = cs.city
        cats = cs.categories or list(DEFAULT_CATEGORIES)
        cap = cs.max_companies
        backend = cs.local_backend or "osm"
        if backend == "gmaps":
            from leadgen.sources.gmaps import GmapsClient

            client = self.gmaps or GmapsClient()
            ok, msg = client.available()
            if not ok:
                ctx.log(f"Google Maps backend unavailable: {msg}", "error")
                return
            source_name = "Google Maps"
        else:
            client = self.client
            source_name = "OpenStreetMap"
        ctx.log(f"querying {source_name} for {', '.join(cats)} businesses in {city or country}"
                + (" (this runs a Docker scrape and can take a few minutes)" if backend == "gmaps" else "") + "...")
        try:
            businesses = await asyncio.to_thread(client.businesses, country, city, cats, cap)
        except Exception as e:  # noqa: BLE001 - surface a clean message, don't crash the run
            ctx.log(f"{source_name} query failed: {e}", "error")
            return
        ctx.log(f"{source_name} returned {len(businesses)} businesses")
        seen: set[str] = set()
        n = 0
        for b in businesses:
            if n >= cap:
                break
            key = company_key_from_name(f"{b.name}-{b.phone or b.osm_id}")
            if key in seen:
                continue
            seen.add(key)
            ctx.check_cancelled()
            loc = ", ".join(x for x in (b.city, b.country) if x) or None
            person = Person(
                key=key, username=b.osm_id, profile_url=b.website or b.maps_url,
                full_name=b.name, raw_title=b.category.title(), headline=b.category.title(),
                location=loc, company_key=company_key_from_name(b.name), company_name=b.name,
                phone=b.whatsapp or b.phone, email=b.email, website=b.website, found_via=[f"local:{b.category}"],
                parse_method="fake",
            )
            company = Company(key=person.company_key, name=b.name, website=b.website, hq=loc,
                              industry=b.category.title(), discovered_via=[f"osm:{b.category}"])
            n += 1
            yield person, company

    # --- unused by the local flow, present to satisfy the adapter shape ---
    async def discover_companies(self, ctx: RunContext):
        return
        yield  # pragma: no cover

    async def profile_company(self, company: Company, ctx: RunContext) -> Company:
        return company

    async def enumerate_people(self, company: Company, ctx: RunContext, known: set[str]):
        return
        yield  # pragma: no cover

    async def enrich_person(self, person: Person, ctx: RunContext) -> Person:
        return person

    async def close(self) -> None:
        return None
