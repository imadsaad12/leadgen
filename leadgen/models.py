"""Pydantic models shared by the pipeline, the store, the exports and the web UI."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Department(StrEnum):
    ENGINEERING = "engineering"
    DATA = "data"
    PRODUCT = "product"
    DESIGN = "design"
    SALES = "sales"
    MARKETING = "marketing"
    CUSTOMER_SUCCESS = "customer_success"
    SUPPORT = "support"
    OPERATIONS = "operations"
    FINANCE = "finance"
    HR = "hr"
    LEGAL = "legal"
    IT = "it"
    EXECUTIVE = "executive"
    RESEARCH = "research"
    CONSULTING = "consulting"
    ADMIN = "admin"
    OTHER = "other"
    UNCLASSIFIED = "unclassified"


class Seniority(StrEnum):
    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    VP = "vp"
    CXO = "cxo"
    FOUNDER = "founder"
    UNKNOWN = "unknown"


SENIORITY_ORDER: list[Seniority] = [
    Seniority.INTERN,
    Seniority.ENTRY,
    Seniority.MID,
    Seniority.SENIOR,
    Seniority.LEAD,
    Seniority.MANAGER,
    Seniority.DIRECTOR,
    Seniority.VP,
    Seniority.CXO,
    Seniority.FOUNDER,
    Seniority.UNKNOWN,
]

# LinkedIn's fixed company-size buckets as they appear on the about page.
SIZE_BUCKETS: list[str] = [
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1,000",
    "1,001-5,000",
    "5,001-10,000",
    "10,001+",
]

SIZE_BUCKET_MIDPOINT: dict[str, int] = {
    "1-10": 5,
    "11-50": 30,
    "51-200": 125,
    "201-500": 350,
    "501-1,000": 750,
    "1,001-5,000": 3000,
    "5,001-10,000": 7500,
    "10,001+": 15000,
}

NetworkDegree = Literal["F", "S", "O"]


def company_key_from_name(name: str) -> str:
    import re as _re
    slug = _re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return f'name:{slug}' if slug else 'name:unknown'


def _clean_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


# --------------------------------------------------------------------------- spec


class YearRange(BaseModel):
    min: int | None = None
    max: int | None = None


class EnrichSpec(BaseModel):
    enabled: bool = False
    max_per_company: int = Field(default=10, ge=0, le=200)


class CompanySpec(BaseModel):
    industries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    employee_size: list[str] = Field(default_factory=list)
    founded_year: YearRange = Field(default_factory=YearRange)
    company_types: list[str] = Field(default_factory=list)
    discover_via_jobs: bool = False
    seed_companies: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    max_companies: int = Field(default=20, ge=1, le=500)
    max_company_queries: int = Field(default=10, ge=1, le=100)
    max_search_pages_per_query: int = Field(default=2, ge=1, le=100)
    city: str | None = None               # local-business source: a city to geocode (e.g. "Beirut")
    categories: list[str] = Field(default_factory=list)  # local-business source: category groups
    local_backend: str = "osm"            # "osm" (free) or "gmaps" (self-hosted Google Maps scraper)

    _clean = field_validator(
        "industries",
        "keywords",
        "exclude_keywords",
        "locations",
        "employee_size",
        "company_types",
        "seed_companies",
        "exclude_companies",
    )(lambda cls, v: _clean_list(v))

    @field_validator("employee_size")
    @classmethod
    def _valid_buckets(cls, v: list[str]) -> list[str]:
        bad = [b for b in v if b not in SIZE_BUCKETS]
        if bad:
            raise ValueError(f"unknown employee_size bucket(s) {bad}; valid: {SIZE_BUCKETS}")
        return v


class PeopleSpec(BaseModel):
    titles: list[str] = Field(default_factory=list)
    title_facets: list[str] = Field(default_factory=list)  # verbatim people-search queries (one facet each)
    exclude_titles: list[str] = Field(default_factory=list)
    departments: list[Department] = Field(default_factory=list)
    seniorities: list[Seniority] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    network: list[NetworkDegree] = Field(default_factory=list)
    include_anonymized: bool = False
    max_per_company: int = Field(default=25, ge=1, le=5000)
    max_pages_per_facet: int = Field(default=10, ge=1, le=100)
    broad_sweep: bool = False  # targeted roles only; set true to also sweep every department
    enrich: EnrichSpec = Field(default_factory=EnrichSpec)

    _clean = field_validator("titles", "exclude_titles", "locations", "keywords")(lambda cls, v: _clean_list(v))


class RunSpec(BaseModel):
    source: str = "linkedin"
    strategy: Literal["people_first", "company_first"] = "people_first"
    daily_budget: int = Field(default=100, ge=1, le=10000)
    monthly_search_cap: int = Field(default=250, ge=1, le=100000)
    min_delay_s: float = Field(default=8.0, ge=0)
    max_delay_s: float = Field(default=20.0, ge=0)
    break_every: int = Field(default=25, ge=1)
    break_min_s: float = Field(default=120.0, ge=0)
    break_max_s: float = Field(default=300.0, ge=0)
    run_hours: tuple[str, str] | None = None
    llm_assist: bool | None = None  # None = enabled when the claude CLI is available


class Spec(BaseModel):
    name: str = "untitled"
    offer: str = ""  # what you are selling and who the ideal buyer is; drives the post-run relevance filter
    company: CompanySpec = Field(default_factory=CompanySpec)
    people: PeopleSpec = Field(default_factory=PeopleSpec)
    run: RunSpec = Field(default_factory=RunSpec)

    def capability_matrix(self) -> dict[str, str]:
        """Which spec fields LinkedIn honours natively, which we post-filter, which are ignored."""
        return {
            "company.industries": "keyword query + post-filter on about page",
            "company.keywords": "keyword query",
            "company.exclude_keywords": "post-filter on name/tagline/about",
            "company.locations": "keyword query + post-filter on HQ",
            "company.employee_size": "post-filter on about page",
            "company.founded_year": "post-filter on about page (often missing)",
            "company.company_types": "post-filter on about page (often missing)",
            "company.discover_via_jobs": "native (job search by keyword + location)",
            "company.seed_companies": "native (direct profile fetch)",
            "people.titles": "native (search keywords, facet)",
            "people.departments": "native (department keyword facets) + classification",
            "people.seniorities": "classification only (post-filter)",
            "people.locations": "native (search location filter)",
            "people.keywords": "native (search keywords)",
            "people.network": "native (connection degree filter)",
        }


# --------------------------------------------------------------------------- entities


CompanyStatus = Literal[
    "discovered", "profiled", "filtered_out", "enumerating", "enumerated", "failed"
]


class Company(BaseModel):
    key: str
    name: str
    slug: str | None = None
    linkedin_url: str | None = None
    urn: str | None = None
    website: str | None = None
    industry: str | None = None
    hq: str | None = None
    size_bucket: str | None = None
    employee_count_hint: int | None = None
    founded: int | None = None
    company_type: str | None = None
    specialties: list[str] = Field(default_factory=list)
    tagline: str | None = None
    followers: int | None = None
    about: str | None = None
    discovered_via: list[str] = Field(default_factory=list)
    function_breakdown: dict[str, int] = Field(default_factory=dict)
    status: CompanyStatus = "discovered"
    filter_reasons: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    people_found: int = 0
    page_loads: int = 0

    @property
    def size_midpoint(self) -> int | None:
        if self.employee_count_hint:
            return self.employee_count_hint
        if self.size_bucket:
            return SIZE_BUCKET_MIDPOINT.get(self.size_bucket)
        return None


ParseMethod = Literal["heuristic", "llm", "reference_only", "fake"]


class ExperienceEntry(BaseModel):
    title: str | None = None
    company: str | None = None
    dates: str | None = None
    location: str | None = None


class Person(BaseModel):
    key: str
    username: str
    profile_url: str
    full_name: str
    headline: str | None = None
    raw_title: str | None = None
    location: str | None = None
    degree: str | None = None
    company_key: str
    company_name: str | None = None
    phone: str | None = None            # local-business source: public phone/WhatsApp
    email: str | None = None            # local-business source: scraped from the website
    website: str | None = None          # public website
    anonymized: bool = False
    found_via: list[str] = Field(default_factory=list)
    parse_method: ParseMethod = "heuristic"
    needs_review: bool = False
    experience: list[ExperienceEntry] | None = None
    relevant: bool | None = None       # set by the post-run relevance filter against the spec's offer
    fit_score: int | None = None       # 0-100 fit to the offer
    relevance_reason: str | None = None


ClassifyMethod = Literal["rule", "llm", "manual", "override", "none"]


class TitleClassification(BaseModel):
    raw_title: str
    normalized: str
    canonical_title: str | None = None
    department: Department = Department.UNCLASSIFIED
    seniority: Seniority = Seniority.UNKNOWN
    function_tags: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    method: ClassifyMethod = "none"

    @property
    def group_key(self) -> str:
        return f"{self.department.value}/{self.seniority.value}"


class Lead(BaseModel):
    person: Person
    company: Company
    classification: TitleClassification
    matched_spec_titles: list[str] = Field(default_factory=list)

    @property
    def group_key(self) -> str:
        return self.classification.group_key


class TitleGroup(BaseModel):
    group_key: str
    department: Department | None = None
    seniority: Seniority | None = None
    canonical_title: str | None = None
    count: int = 0
    by_company: dict[str, int] = Field(default_factory=dict)
    top_raw_titles: list[tuple[str, int]] = Field(default_factory=list)
    lead_keys: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- runs


RunStatus = Literal["queued", "running", "paused", "done", "failed", "stopped"]


class RunStats(BaseModel):
    page_loads: int = 0
    cache_hits: int = 0
    companies_discovered: int = 0
    companies_passed: int = 0
    companies_enumerated: int = 0
    people_found: int = 0
    people_classified: int = 0
    people_unclassified: int = 0
    llm_calls: int = 0
    errors: int = 0


class RunState(BaseModel):
    id: str
    spec: Spec
    status: RunStatus = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: RunStats = Field(default_factory=RunStats)
    cursor: dict[str, str] = Field(default_factory=dict)
    message: str | None = None


class Event(BaseModel):
    run_id: str | None
    ts: datetime
    level: Literal["debug", "info", "warning", "error"]
    msg: str
    data: dict | None = None
