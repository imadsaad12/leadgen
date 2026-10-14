"""FakeSource: deterministic synthetic companies and people rendered as LinkedIn-style raw pages.

It exercises the real parsers (extract/*) so tests and UI development never need a LinkedIn session.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any

from leadgen.extract.companies import parse_company_about, parse_company_search
from leadgen.extract.people import parse_people_page
from leadgen.models import Company, Person, company_key_from_name
from leadgen.sources.base import RunContext
from leadgen.sources.queries import company_queries, people_facets

COMPANIES: list[dict[str, Any]] = [
    {"slug": "northwind-pay", "name": "Northwind Pay", "industry": "Financial Services", "hq": "Berlin, Berlin",
     "size": "51-200", "founded": 2016, "type": "Privately Held", "urn": "71001", "n": 140,
     "tagline": "Embedded payments for European marketplaces", "website": "https://northwindpay.example"},
    {"slug": "lumen-health", "name": "Lumen Health", "industry": "Hospitals and Health Care", "hq": "Munich, Bavaria",
     "size": "201-500", "founded": 2011, "type": "Privately Held", "urn": "71002", "n": 320,
     "tagline": "Digital clinics for chronic care", "website": "https://lumenhealth.example"},
    {"slug": "ferrum-logistics", "name": "Ferrum Logistics", "industry": "Transportation, Logistics, Supply Chain and Storage",
     "hq": "Dubai, United Arab Emirates", "size": "501-1,000", "founded": 2005, "type": "Privately Held", "urn": "71003",
     "n": 800, "tagline": "Cross-border freight across the GCC", "website": "https://ferrum.example"},
    {"slug": "quanta-ai", "name": "Quanta AI", "industry": "Software Development", "hq": "San Francisco, California",
     "size": "11-50", "founded": 2021, "type": "Privately Held", "urn": "71004", "n": 38,
     "tagline": "LLM infrastructure for regulated industries", "website": "https://quanta.example"},
    {"slug": "solstice-energy", "name": "Solstice Energy", "industry": "Renewable Energy Semiconductor Manufacturing",
     "hq": "Madrid, Community of Madrid", "size": "1,001-5,000", "founded": 1998, "type": "Public Company", "urn": "71005",
     "n": 2400, "tagline": "Utility-scale solar and storage", "website": "https://solstice.example"},
    {"slug": "bluefin-saas", "name": "Bluefin", "industry": "Software Development", "hq": "London, England",
     "size": "201-500", "founded": 2014, "type": "Privately Held", "urn": "71006", "n": 260,
     "tagline": "Revenue intelligence for B2B sales teams", "website": "https://bluefin.example"},
    {"slug": "atlas-recruiting", "name": "Atlas Recruiting", "industry": "Staffing and Recruiting", "hq": "Amsterdam, North Holland",
     "size": "11-50", "founded": 2018, "type": "Privately Held", "urn": None, "n": 30,
     "tagline": "Tech recruiting across Benelux", "website": "https://atlas.example"},
    {"slug": "cedar-fintech", "name": "Cedar", "industry": "Financial Services", "hq": "Beirut, Lebanon",
     "size": "51-200", "founded": 2019, "type": "Privately Held", "urn": "71008", "n": 90,
     "tagline": "Neobank for freelancers in MENA", "website": "https://cedar.example"},
]

FIRST = ["Lina", "Omar", "Sara", "Jonas", "Maya", "Karim", "Elena", "Tom", "Nour", "Felix", "Aisha", "Lucas", "Hana",
         "Mateo", "Yara", "Noah", "Leila", "Ben", "Rania", "Paul", "Dana", "Ali", "Sofia", "Max", "Zeina", "Jan"]
LAST = ["Haddad", "Müller", "Rossi", "Khalil", "Schmidt", "Garcia", "Nasser", "Becker", "Saleh", "Fischer", "Costa",
        "Mansour", "Weber", "Silva", "Aoun", "Wagner", "Fernandez", "Hariri", "Koch", "Moreno", "Said", "Richter"]
TITLES_BY_DEPT: dict[str, list[str]] = {
    "engineering": ["Software Engineer", "Senior Software Engineer", "Backend Developer", "Staff Engineer", "DevOps Engineer",
                    "Engineering Manager", "Head of Engineering", "CTO", "QA Engineer", "Frontend Developer (React)",
                    "Werkstudent Softwareentwicklung", "Principal Architect", "Mobile Developer iOS"],
    "sales": ["Account Executive", "Senior Account Executive", "Sales Development Representative", "Head of Sales",
              "VP Sales", "Business Development Manager", "Key Account Manager", "Regional Sales Director",
              "Chief Revenue Officer", "Partnerships Lead", "Sales Engineer", "Vertriebsleiter"],
    "marketing": ["Marketing Manager", "Content Marketing Lead", "Head of Marketing", "Growth Marketing Manager",
                  "Brand Manager", "CMO", "Performance Marketing Specialist", "Communications Manager", "SEO Specialist"],
    "product": ["Product Manager", "Senior Product Manager", "Head of Product", "Product Owner", "CPO", "Product Analyst"],
    "operations": ["Operations Manager", "COO", "Project Manager", "Supply Chain Analyst", "Logistics Coordinator",
                   "Head of Operations", "Scrum Master", "Procurement Specialist", "Chief of Staff"],
    "finance": ["Financial Controller", "CFO", "Senior Accountant", "FP&A Manager", "Finance Director", "Treasury Analyst"],
    "hr": ["Recruiter", "Talent Acquisition Partner", "HR Business Partner", "Head of People", "Chief People Officer",
           "People Operations Specialist", "Technical Recruiter"],
    "customer_success": ["Customer Success Manager", "Head of Customer Success", "Onboarding Specialist", "Account Manager"],
    "support": ["Customer Support Specialist", "Technical Support Engineer", "Support Team Lead"],
    "data": ["Data Scientist", "Data Engineer", "Head of Data", "Machine Learning Engineer", "Data Analyst", "Research Scientist, NLP"],
    "design": ["Product Designer", "UX Designer", "Head of Design", "Graphic Designer"],
    "it": ["IT Support Specialist", "System Administrator", "Information Security Analyst", "IT Manager"],
    "legal": ["Legal Counsel", "General Counsel", "Compliance Officer"],
    "executive": ["CEO", "Co-Founder & CEO", "Managing Director", "Founder", "Gründer & Geschäftsführer"],
    "admin": ["Executive Assistant to the CEO", "Office Manager"],
    "research": ["Clinical Research Associate", "Research Fellow"],
    "other": ["Intergalactic Vibes Curator", "Chief Happiness Wizard", "Jefe de Nada"],
}
DEPT_WEIGHTS = {"engineering": 30, "sales": 18, "marketing": 8, "product": 6, "operations": 8, "finance": 5, "hr": 5,
                "customer_success": 5, "support": 4, "data": 5, "design": 3, "it": 3, "legal": 1, "executive": 2,
                "admin": 1, "research": 1, "other": 2}
LOCATIONS = ["Berlin, Germany", "Munich, Bavaria, Germany", "Dubai, United Arab Emirates", "London, England, United Kingdom",
             "Beirut, Lebanon", "Madrid, Spain", "Amsterdam, North Holland, Netherlands", "San Francisco Bay Area", "Remote"]


def _people_for(company: dict[str, Any]) -> list[dict[str, Any]]:
    rng = random.Random(company["slug"])
    depts = list(DEPT_WEIGHTS)
    weights = [DEPT_WEIGHTS[d] for d in depts]
    n = min(company["n"], 120)
    people = []
    used: set[str] = set()
    for _ in range(n):
        dept = rng.choices(depts, weights)[0]
        title = rng.choice(TITLES_BY_DEPT[dept])
        first, last = rng.choice(FIRST), rng.choice(LAST)
        base = f"{first}-{last}".lower().replace("ü", "ue").replace("ö", "oe")
        username = base
        while username in used:
            username = f"{base}-{rng.randint(10, 99)}{rng.choice('abcdef')}"
        used.add(username)
        headline = f"{title} at {company['name']}" if rng.random() < 0.8 else f"{title} | {rng.choice(['Speaker', 'Mentor', 'Angel investor', 'ex-Google'])}"
        people.append({"username": username, "name": f"{first} {last}", "dept": dept, "title": title,
                       "headline": headline, "location": rng.choice(LOCATIONS), "degree": rng.choice(["2nd", "3rd+", "2nd", "1st"])})
    return people


def people_page_payload(company: dict[str, Any], people: list[dict[str, Any]], page: int, total: int,
                        kind: str = "search") -> dict[str, Any]:
    lines = [f"About {total} results" if kind == "search" else f"{total} employees"]
    if kind == "employees":
        lines += [f"{total:,} associated members", "Page 1 of 6",
                  "Where they live", "Add", "40 Germany", "toggle off", "",
                  "What they do", "41 Engineering", "toggle off", "", "25 Sales", "toggle off", "",
                  "12 Marketing", "toggle off", "", "10 Operations", "toggle off", "",
                  "What they are skilled at", "30 Python", "toggle off"]
    refs = []
    for p in people:
        lines += [p["name"], f"View {p['name']}'s profile", f"• {p['degree']}", p["headline"], p["location"],
                  "2 mutual connections", "Connect"]
        refs.append({"kind": "person", "url": f"/in/{p['username']}/", "text": p["name"]})
    if page == 1 and kind == "search":
        lines += ["LinkedIn Member", "• 3rd+", "Engineer at Somewhere", "Germany"]
        refs.append({"kind": "person", "url": "/in/ACoAABxyz123/", "text": "LinkedIn Member"})
    section = "search_results" if kind == "search" else "employees"
    return {"url": f"https://www.linkedin.com/fake/{company['slug']}/{kind}?page={page}",
            "sections": {section: "\n".join(lines)}, "references": {section: refs}}


def about_payload(company: dict[str, Any]) -> dict[str, Any]:
    lines = [company["name"], company["tagline"], company["industry"], company["hq"], "12K followers",
             f"{company['size']} employees", f"See all {company['n']} employees", "Overview", company["tagline"] + ".",
             "Website", company["website"], "Industry", company["industry"], "Company size", f"{company['size']} employees",
             "Headquarters", company["hq"], "Type", company["type"], "Founded", str(company["founded"]),
             "Specialties", "payments, api, risk"]
    refs = [{"kind": "company", "url": f"/company/{company['slug']}/", "text": company["name"]}]
    if company["urn"]:
        refs.append({"kind": "company_urn", "url": f"/search/results/people/?currentCompany=%5B%22{company['urn']}%22%5D",
                     "value": company["urn"]})
    return {"url": f"https://www.linkedin.com/company/{company['slug']}/about/", "sections": {"about": "\n".join(lines)},
            "references": {"about": refs}}


def company_search_payload(companies: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [f"{len(companies)} results"]
    refs = []
    for c in companies:
        lines += [c["name"], f"{c['industry']} • {c['hq']}", "12K followers", c["tagline"], "3 jobs", "Follow"]
        refs.append({"kind": "company", "url": f"/company/{c['slug']}/", "text": c["name"]})
    return {"url": "https://www.linkedin.com/fake/search/companies", "sections": {"search_results": "\n".join(lines)},
            "references": {"search_results": refs}}


class FakeSource:
    name = "fake"
    native_company_filters = frozenset()
    native_people_filters = frozenset()

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._by_slug = {c["slug"]: c for c in COMPANIES}

    def _match(self, q: str, c: dict[str, Any]) -> bool:
        words = [w for w in q.lower().replace('"', "").split() if len(w) > 2 and w not in ("and", "the", "for")]
        hay = f"{c['name']} {c['industry']} {c['tagline']} {c['hq']}".lower()
        return any(w in hay for w in words) if words else True

    async def discover_companies(self, ctx: RunContext) -> AsyncIterator[Company]:
        seen: set[str] = set()
        for seed in ctx.spec.company.seed_companies:
            slug = seed.strip().strip("/").split("/")[-1].lower()
            if slug in self._by_slug and slug not in seen:
                seen.add(slug)
                yield Company(key=slug, name=self._by_slug[slug]["name"], slug=slug, discovered_via=["seed"])
        for q in company_queries(ctx.spec):
            hits = [c for c in COMPANIES if self._match(q, c) and c["slug"] not in seen]
            self.calls.append(("search_companies", {"keywords": q}))
            parsed = parse_company_search(company_search_payload(hits), found_via=f"search:{q}")
            for c in parsed.companies:
                if c.key not in seen:
                    seen.add(c.key)
                    yield c

    async def profile_company(self, company: Company, ctx: RunContext) -> Company:
        data = self._by_slug.get(company.key)
        if not data:
            return company
        self.calls.append(("get_company_profile", {"company_name": company.key}))
        return parse_company_about(about_payload(data), company)

    async def search_leads(self, ctx: RunContext):
        """People-first fake: yield (Person, Company) for decision-makers in the spec's countries/domains."""
        from leadgen.pipeline.geo import canonical_country, location_country

        ps = ctx.spec.people
        wanted = {c for c in (canonical_country(loc) for loc in ctx.spec.company.locations) if c}
        roles = [t.lower() for t in ps.titles]
        seen: set[str] = set()
        for data in COMPANIES:
            if wanted:
                cc = location_country(data["hq"])
                if cc and cc not in wanted:
                    continue
            for person_d in _people_for(data):
                ctx.check_cancelled()
                title = person_d["title"].lower()
                if roles and not any(r.split()[0] in title or title in r for r in roles):
                    continue
                username = person_d["username"]
                if username in seen:
                    continue
                seen.add(username)
                self.calls.append(("search_people", {"keywords": person_d["title"], "geo_urn": "fake"}))
                cname = data["name"]
                p = Person(key=username, username=username, profile_url=f"https://www.linkedin.com/in/{username}/",
                           full_name=person_d["name"], headline=person_d["headline"], raw_title=person_d["title"],
                           location=person_d["location"], degree=person_d["degree"],
                           company_key=company_key_from_name(cname), company_name=cname, found_via=["lead:fake"])
                yield p, Company(key=company_key_from_name(cname), name=cname, discovered_via=["lead:fake"])

    async def enumerate_people(self, company: Company, ctx: RunContext, known: set[str]) -> AsyncIterator[Person]:
        data = self._by_slug.get(company.key)
        if not data:
            return
        people = _people_for(data)
        cap = ctx.spec.people.max_per_company
        yielded = 0
        # people tab first
        self.calls.append(("get_company_employees", {"company_name": company.key}))
        parsed = parse_people_page(people_page_payload(data, people[:10], 1, data["n"], "employees"),
                                   company_key=company.key, company_name=company.name, found_via="employees")
        company.function_breakdown = parsed.function_breakdown
        company.employee_count_hint = parsed.total_hint
        for p in parsed.people:
            if p.key not in known and not p.anonymized:
                known.add(p.key)
                yielded += 1
                yield p
        facets = people_facets(ctx.spec, company.function_breakdown)
        for facet in facets:
            if yielded >= cap:
                return
            ctx.check_cancelled()
            words = [w for w in facet.keywords.lower().replace('"', "").replace(" or ", " ").split()] if facet.keywords else []
            pool = [p for p in people if not words or any(w in p["title"].lower() or w in p["dept"] for w in words)]
            for page in range(1, ctx.spec.people.max_pages_per_facet + 1):
                chunk = pool[(page - 1) * 10: page * 10]
                if not chunk or yielded >= cap:
                    break
                self.calls.append(("search_people", {"keywords": facet.keywords, "page": page}))
                parsed = parse_people_page(people_page_payload(data, chunk, page, len(pool)),
                                           company_key=company.key, company_name=company.name, found_via=facet.label)
                new = 0
                for p in parsed.people:
                    if p.key in known or (p.anonymized and not ctx.spec.people.include_anonymized):
                        continue
                    known.add(p.key)
                    new += 1
                    yielded += 1
                    yield p
                    if yielded >= cap:
                        break
                ctx.store.update_facet(ctx.run_id, company.key, facet.id, last_page=page, status="running", new_found=new)
                if new == 0 and page > 1:
                    break
            ctx.store.update_facet(ctx.run_id, company.key, facet.id, last_page=0, status="done", new_found=0)

    async def enrich_person(self, person: Person, ctx: RunContext) -> Person:
        return person

    async def call(self, tool, args, **kw):
        """Minimal MCP-like shim so scrape_profile() works against the fake source in tests."""
        self.calls.append((tool, args))
        if tool == "get_person_profile":
            u = args.get("linkedin_username", "jane-doe")
            name = u.replace("-", " ").title()
            return {"url": f"https://www.linkedin.com/in/{u}/",
                    "sections": {"main_profile": f"{name}\nHead of Engineering at Northwind Pay\nBerlin, Germany",
                                 "experience": "Head of Engineering\nNorthwind Pay · Full-time\nJan 2021 - Present · Berlin\n\nSenior Engineer\nAcme\n2018 - 2021",
                                 "education": "TU Berlin\nMSc Computer Science",
                                 "skills": "Python\nDistributed Systems\nLeadership",
                                 "contact_info": "Email\njane.doe@northwindpay.example\nPhone\n+49 170 1234567 (Mobile)\nWebsite\nhttps://janedoe.example"}}
        return {"url": "x", "sections": {}}

    async def close(self) -> None:
        return None
