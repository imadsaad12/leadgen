"""Turn a Spec into LinkedIn keyword queries (company discovery) and people-search facets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from leadgen.config import DATA_DIR
from leadgen.models import Department, Seniority, Spec

DEPARTMENT_FACETS: dict[Department, list[str]] = {
    Department.ENGINEERING: ['engineer OR developer OR software', 'devops OR architect OR "quality assurance" OR qa'],
    Department.DATA: ['data OR analytics OR "machine learning" OR scientist'],
    Department.PRODUCT: ['"product manager" OR "product owner" OR "head of product"'],
    Department.DESIGN: ['designer OR ux OR "user experience" OR creative'],
    Department.SALES: ['sales OR "account executive" OR "business development"', '"account manager" OR partnerships OR revenue'],
    Department.MARKETING: ['marketing OR brand OR content OR communications OR growth'],
    Department.CUSTOMER_SUCCESS: ['"customer success" OR onboarding OR "account management" OR "customer experience"'],
    Department.SUPPORT: ['support OR "customer service" OR helpdesk'],
    Department.OPERATIONS: ['operations OR logistics OR "supply chain" OR "project manager" OR procurement'],
    Department.FINANCE: ['finance OR accounting OR controller OR treasury'],
    Department.HR: ['recruiter OR "human resources" OR "talent acquisition" OR people OR hr'],
    Department.LEGAL: ['legal OR counsel OR compliance'],
    Department.IT: ['"information technology" OR security OR infrastructure OR "system administrator" OR it'],
    Department.EXECUTIVE: ['ceo OR founder OR "managing director" OR president OR coo OR cfo OR cto OR "general manager"'],
    Department.RESEARCH: ['research OR scientist OR phd'],
    Department.CONSULTING: ['consultant OR advisor'],
    Department.ADMIN: ['assistant OR office OR administrative'],
}

SENIORITY_FACETS: dict[Seniority, str] = {
    Seniority.CXO: 'chief OR ceo OR cto OR cfo OR coo OR cmo OR president OR "managing director"',
    Seniority.FOUNDER: 'founder OR "co-founder" OR owner',
    Seniority.VP: '"vice president" OR vp OR svp OR evp',
    Seniority.DIRECTOR: 'director OR "head of"',
    Seniority.MANAGER: 'manager OR "team lead" OR supervisor',
    Seniority.LEAD: 'lead OR principal OR staff',
    Seniority.SENIOR: 'senior',
    Seniority.ENTRY: 'junior OR associate OR assistant',
    Seniority.INTERN: 'intern OR "working student" OR trainee',
}

DEFAULT_SWEEP_ORDER = [
    Department.EXECUTIVE, Department.SALES, Department.MARKETING, Department.ENGINEERING, Department.PRODUCT,
    Department.OPERATIONS, Department.FINANCE, Department.HR, Department.CUSTOMER_SUCCESS, Department.DATA,
    Department.IT, Department.DESIGN, Department.SUPPORT, Department.LEGAL, Department.RESEARCH,
    Department.CONSULTING, Department.ADMIN,
]


@dataclass(frozen=True)
class Facet:
    id: str
    label: str
    keywords: str
    location: str | None = None
    network: tuple[str, ...] | None = None
    priority: int = 0

    def args(self, urn: str | None) -> dict:
        return {
            "keywords": self.keywords,
            "current_company": urn,
            "location": self.location,
            "network": list(self.network) if self.network else None,
        }


def _fid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


@lru_cache(maxsize=1)
def industry_synonyms() -> dict[str, dict]:
    path = Path(DATA_DIR) / "industry_synonyms.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).lower(): v or {} for k, v in (data.get("synonyms") or {}).items()}


def industry_terms(industry: str) -> list[str]:
    """Keywords to search for an industry the user typed (synonyms first, then the literal)."""
    key = industry.strip().lower()
    syn = industry_synonyms().get(key)
    terms: list[str] = []
    if syn:
        terms.extend(k for k in syn.get("keywords", []) if k)
    if key not in [t.lower() for t in terms]:
        terms.insert(0, industry.strip())
    return terms[:3]


def company_queries(spec: Spec) -> list[str]:
    cs = spec.company
    bases: list[str] = []
    for ind in cs.industries:
        bases.extend(industry_terms(ind))
    bases.extend(cs.keywords)
    if not bases:
        return []
    locs = cs.locations or [None]
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = " ".join(q.split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    for base in bases:
        for loc in locs:
            add(f"{base} {loc}" if loc else base)
    for ind in cs.industries:
        for kw in cs.keywords:
            add(f"{ind} {kw}")
    for base in bases:
        add(base)
    return queries[: cs.max_company_queries]


def _quote(t: str) -> str:
    t = t.strip().strip('"')
    return f'"{t}"' if " " in t else t


def people_facets(spec: Spec, function_breakdown: dict[str, int] | None = None) -> list[Facet]:
    ps = spec.people
    locs: list[str | None] = list(ps.locations) or [None]
    nets: list[tuple[str, ...] | None] = [tuple(ps.network)] if ps.network else [None]
    facets: list[Facet] = []
    prio = 0

    def add(label: str, keywords: str, kind: str) -> None:
        nonlocal prio
        for loc in locs:
            for net in nets:
                prio += 1
                facets.append(Facet(_fid(kind, keywords, loc or "", ",".join(net or ())), label, keywords, loc, net, prio))

    # 0. explicit pre-grouped facets (used by the company decision-maker scraper): one search each, verbatim
    if ps.title_facets:
        for fstr in ps.title_facets:
            add(f"roles: {fstr[:40]}...", fstr, "facet")
        for kw in ps.keywords:
            add(f"keyword: {kw}", _quote(kw), "kw")
        return facets
    # 1. explicit titles, OR-joined three at a time
    titles = [t for t in ps.titles if t]
    for i in range(0, len(titles), 3):
        chunk = titles[i : i + 3]
        add("titles: " + ", ".join(chunk), " OR ".join(_quote(t) for t in chunk), "title")
    # 2. department facets (requested ones, else - if sweeping - all, ordered by the company's function breakdown)
    deps = list(ps.departments)
    if not deps and ps.broad_sweep:
        deps = list(DEFAULT_SWEEP_ORDER)
        if function_breakdown:
            ranked = sorted(function_breakdown.items(), key=lambda kv: -kv[1])
            front: list[Department] = []
            for name, _ in ranked:
                dep = _breakdown_to_department(name)
                if dep and dep in deps and dep not in front:
                    front.append(dep)
            deps = front + [d for d in deps if d not in front]
    for dep in deps:
        for q in DEPARTMENT_FACETS.get(dep, []):
            add(f"department: {dep.value}", q, "dept")
    # 3. seniority facets when the spec targets levels but gave no titles
    if ps.seniorities and not titles:
        for sen in ps.seniorities:
            q = SENIORITY_FACETS.get(sen)
            if q:
                add(f"seniority: {sen.value}", q, "sen")
    # 4. free keywords
    for kw in ps.keywords:
        add(f"keyword: {kw}", _quote(kw), "kw")
    # 5. broad sweep (empty keywords, company facet only)
    if ps.broad_sweep:
        add("all employees", "", "all")
    return facets


_BREAKDOWN_MAP = {
    "engineering": Department.ENGINEERING, "information technology": Department.IT, "sales": Department.SALES,
    "business development": Department.SALES, "marketing": Department.MARKETING, "operations": Department.OPERATIONS,
    "finance": Department.FINANCE, "human resources": Department.HR, "product management": Department.PRODUCT,
    "research": Department.RESEARCH, "support": Department.SUPPORT, "customer success": Department.CUSTOMER_SUCCESS,
    "arts and design": Department.DESIGN, "legal": Department.LEGAL, "consulting": Department.CONSULTING,
    "administrative": Department.ADMIN, "program and project management": Department.OPERATIONS,
    "accounting": Department.FINANCE, "quality assurance": Department.ENGINEERING, "media and communication": Department.MARKETING,
    "entrepreneurship": Department.EXECUTIVE, "education": Department.RESEARCH, "healthcare services": Department.RESEARCH,
    "purchasing": Department.OPERATIONS, "real estate": Department.SALES, "community and social services": Department.OTHER,
}


def _breakdown_to_department(name: str) -> Department | None:
    return _BREAKDOWN_MAP.get(name.strip().lower())


def estimate_page_loads(spec: Spec, n_companies: int | None = None) -> dict[str, int]:
    """Rough plan used by dry runs: how many LinkedIn page loads a run may cost."""
    cs, ps = spec.company, spec.people
    queries = company_queries(spec)
    discovery = len(queries) * cs.max_search_pages_per_query
    candidates = min(cs.max_companies * 3, discovery * 10 + len(cs.seed_companies)) if queries else len(cs.seed_companies)
    profiles = min(candidates, cs.max_companies * 2) if queries else len(cs.seed_companies)
    companies = n_companies if n_companies is not None else min(cs.max_companies, max(profiles, 1))
    facets = people_facets(spec)
    per_company = 1 + min(len(facets) * ps.max_pages_per_facet, max(1, -(-ps.max_per_company // 10)) + len(facets))
    people = companies * per_company
    return {
        "company_queries": len(queries),
        "discovery_pages": discovery,
        "profile_pages": profiles,
        "companies": companies,
        "facets_per_company": len(facets),
        "people_pages": people,
        "total": discovery + profiles + people,
    }


@dataclass(frozen=True)
class PeopleQuery:
    id: str
    keywords: str
    country: str | None       # spec term the user typed (country name)
    geo_urn: str | None
    label: str


def lead_queries(spec: Spec) -> list["PeopleQuery"]:
    """People-first discovery: find decision-makers in a location working in a domain, per (role x domain x country).

    Keywords combine a target role with the domain terms, e.g. 'Head of Engineering fintech'. Location is applied
    through LinkedIn's geo facet (geo_urn), which actually filters, unlike free-text location.
    """
    from leadgen.pipeline.geo import geo_urn

    ps, cs = spec.people, spec.company
    domains: list[str] = []
    for ind in cs.industries:
        domains.extend(industry_terms(ind)[:1])  # one representative keyword per industry
    domains.extend(cs.keywords)
    domains = list(dict.fromkeys(d for d in domains if d)) or [""]

    roles = [t for t in ps.titles if t]
    if not roles:
        # no explicit titles: use decision-maker seniority keyword groups (or a bare domain sweep)
        roles = [SENIORITY_FACETS[s] for s in (ps.seniorities or [Seniority.CXO, Seniority.VP, Seniority.DIRECTOR])
                 if s in SENIORITY_FACETS] or [""]

    countries = cs.locations or [None]
    out: list[PeopleQuery] = []
    seen: set[str] = set()
    for country in countries:
        gu = geo_urn(country) if country else None
        for role in roles:
            for domain in domains:
                kw = " ".join(x for x in (_quote(role) if " " in role and role == role.strip('"') else role, domain) if x).strip()
                if not kw:
                    continue
                key = f"{kw}|{gu or country or ''}"
                if key in seen:
                    continue
                seen.add(key)
                label = f"{role or 'any'} / {domain or 'any'}" + (f" in {country}" if country else "")
                out.append(PeopleQuery(_fid('lead', kw, gu or ''), kw, country, gu, label))
    return out[: max(spec.company.max_company_queries, len(roles))]
