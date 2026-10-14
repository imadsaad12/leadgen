"""Deterministic parsers for company search pages and company About pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from leadgen.extract.common import (
    EMPLOYEES_RE,
    SIZE_RE,
    company_slug,
    find_line_index,
    flatten_references,
    is_noise,
    lines_of,
    parse_followers,
)
from leadgen.models import SIZE_BUCKETS, Company

ABOUT_LABELS = {
    "website": "website",
    "industry": "industry",
    "company size": "size",
    "headquarters": "hq",
    "type": "company_type",
    "founded": "founded",
    "specialties": "specialties",
    "phone": "phone",
}
TAGLINE_SEP_RE = re.compile(r"\s*[•·|]\s*")


_FOLLOW_WORDS = {"follow", "following", "+ follow", "message", "visit website"}


@dataclass
class ParsedCompanies:
    companies: list[Company]
    status: str
    notes: list[str] = field(default_factory=list)


def _company_refs(payload: dict[str, Any]) -> list[tuple[str, str | None]]:
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for ref in flatten_references(payload.get("references")):
        if ref.get("kind") not in (None, "company"):
            continue
        slug = company_slug(ref.get("url"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, (ref.get("text") or "").strip() or None))
    return out


def _normalize_bucket(text: str) -> str | None:
    t = text.replace(" ", "").replace(".", ",")
    for b in SIZE_BUCKETS:
        if b.replace(",", "") == t.replace(",", ""):
            return b
    return None


def parse_company_search(payload: dict[str, Any], *, found_via: str = "search") -> ParsedCompanies:
    refs = _company_refs(payload)
    text = (payload.get("sections") or {}).get("search_results") or ""
    lines = lines_of(text)
    companies: list[Company] = []
    cursor = 0
    located = 0
    for slug, name in refs:
        display = name or slug.replace("-", " ").title()
        idx = find_line_index(lines, display, cursor) if name else -1
        c = Company(key=slug, name=display, slug=slug, linkedin_url=f"https://www.linkedin.com/company/{slug}/",
                    discovered_via=[found_via])
        if idx >= 0:
            located += 1
            cursor = idx + 1
            block = [x for x in lines[idx + 1: idx + 9] if x and x.lower() != display.lower()]
            for ln in block:
                f = parse_followers(ln)
                if f is not None:
                    c.followers = f
                    continue
                m = SIZE_RE.search(ln)
                if m and c.size_bucket is None:
                    c.size_bucket = _normalize_bucket(m.group(1))
                    continue
                if is_noise(ln) or re.fullmatch(r"\d+ jobs?", ln, re.I) or ln.lower() in _FOLLOW_WORDS:
                    continue
                # LinkedIn stacks the card as: Industry line (no comma), then Location line (has a comma),
                # then the description. Separator-joined "Industry · City" is an older variant.
                if c.industry is None and TAGLINE_SEP_RE.search(ln) and len(ln) < 120:
                    parts = [p.strip() for p in TAGLINE_SEP_RE.split(ln) if p.strip()]
                    c.industry = parts[0]
                    if len(parts) > 1:
                        c.hq = parts[1]
                    continue
                if c.industry is None and "," not in ln and len(ln) < 60:
                    c.industry = ln
                    continue
                if c.hq is None and "," in ln and len(ln) < 60 and not ln.endswith((".", "!")):
                    c.hq = ln
                    continue
                if c.tagline is None and len(ln) > 15:
                    c.tagline = ln
        companies.append(c)
    status = "ok" if refs and located >= max(1, int(0.6 * len(refs))) else ("empty" if not refs else "degraded")
    notes = [] if status == "ok" else [f"located {located}/{len(refs)} company cards"]
    return ParsedCompanies(companies, status, notes)


def parse_company_about(payload: dict[str, Any], company: Company) -> Company:
    """Fill industry / size / HQ / founded / website / specialties / urn from the About page payload."""
    sections = payload.get("sections") or {}
    text = sections.get("about") or "\n".join(v for v in sections.values() if isinstance(v, str))
    lines = lines_of(text)
    c = company.model_copy(deep=True)
    for i, ln in enumerate(lines):
        key = ABOUT_LABELS.get(ln.strip().lower().rstrip(":"))
        if not key:
            continue
        value = next((x for x in lines[i + 1: i + 3] if x), "")
        if not value:
            continue
        if key == "website" and value.lower().startswith(("http", "www.")) or (key == "website" and "." in value):
            c.website = value
        elif key == "industry":
            c.industry = value
        elif key == "size":
            m = SIZE_RE.search(value)
            c.size_bucket = _normalize_bucket(m.group(1)) if m else c.size_bucket
        elif key == "hq":
            c.hq = value
        elif key == "company_type":
            c.company_type = value
        elif key == "founded":
            m = re.search(r"\b(18|19|20)\d{2}\b", value)
            c.founded = int(m.group(0)) if m else c.founded
        elif key == "specialties":
            c.specialties = [s.strip().rstrip(".") for s in re.split(r",|;| and ", value) if s.strip()]
    for ln in lines[:40]:
        if SIZE_RE.search(ln) and not re.search(r"see all", ln, re.I):
            if c.size_bucket is None:
                c.size_bucket = _normalize_bucket(SIZE_RE.search(ln).group(1))
            continue
        m = EMPLOYEES_RE.search(ln)
        if m:
            try:
                c.employee_count_hint = int(m.group(1).replace(",", "").replace(".", ""))
                break
            except ValueError:
                pass
    if c.tagline is None:
        for ln in lines[1:6]:
            if ln and not is_noise(ln) and ln.lower() != c.name.lower() and len(ln) > 10 and not SIZE_RE.search(ln):
                c.tagline = ln
                break
    overview = find_line_index(lines, "overview")
    if overview >= 0:
        body = [x for x in lines[overview + 1: overview + 15] if x and x.lower() not in ABOUT_LABELS]
        if body:
            c.about = " ".join(body)[:1500]
    for ref in flatten_references(payload.get("references")):
        if ref.get("kind") == "company_urn" and ref.get("value"):
            c.urn = str(ref["value"])
            break
        url = ref.get("url") or ""
        m = re.search(r"currentCompany=%5B%22(\d+)%22%5D|currentCompany=\[\"?(\d+)", url)
        if m:
            c.urn = m.group(1) or m.group(2)
            break
    unknown = [f for f, v in (("industry", c.industry), ("size", c.size_bucket), ("hq", c.hq),
                              ("founded", c.founded), ("urn", c.urn)) if not v]
    c.unknown_fields = unknown
    c.status = "profiled"
    return c


def parse_job_companies(payload: dict[str, Any]) -> list[Company]:
    """Companies that appear in a job-search payload's references."""
    out: list[Company] = []
    for slug, name in _company_refs(payload):
        out.append(Company(key=slug, name=name or slug.replace("-", " ").title(), slug=slug,
                           linkedin_url=f"https://www.linkedin.com/company/{slug}/", discovered_via=["jobs"]))
    return out
