"""Post-filter profiled companies against the spec (size, industry, HQ, founded, type, exclusions)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from leadgen.config import DATA_DIR
from leadgen.models import Company, Spec
from leadgen.pipeline.geo import hq_matches
from leadgen.sources.queries import industry_synonyms


@lru_cache(maxsize=1)
def _industry_aliases() -> dict[str, set[str]]:
    path = Path(DATA_DIR) / "linkedin_industries.yaml"
    out: dict[str, set[str]] = {}
    if not path.exists():
        return out
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in data.get("industries", []):
        name = (entry.get("name") or "").strip().lower()
        if name:
            out[name] = {name} | {a.strip().lower() for a in entry.get("aliases", []) if a}
    return out


def industry_match_terms(spec_industry: str) -> set[str]:
    key = spec_industry.strip().lower()
    terms = {key}
    aliases = _industry_aliases()
    if key in aliases:
        terms |= aliases[key]
    for name, als in aliases.items():
        if key in als:
            terms |= als | {name}
    syn = industry_synonyms().get(key) or {}
    terms |= {s.lower() for s in syn.get("industries", [])}
    terms |= {k.lower() for k in syn.get("keywords", [])}
    return {t for t in terms if t}


def industry_matches(company: Company, spec_industries: list[str]) -> bool | None:
    if not spec_industries:
        return True
    hay_primary = (company.industry or "").lower()
    hay_text = " ".join(filter(None, [company.tagline, company.about, " ".join(company.specialties)])).lower()
    if not hay_primary and not hay_text:
        return None
    for ind in spec_industries:
        for term in industry_match_terms(ind):
            if hay_primary and fuzz.token_set_ratio(term, hay_primary) >= 80:
                return True
            if len(term) >= 4 and re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", hay_text):
                return True
    return False if hay_primary else None


def post_filter(company: Company, spec: Spec) -> tuple[bool, list[str], list[str]]:
    """Return (passed, reasons_for_failure, unknown_fields)."""
    cs = spec.company
    reasons: list[str] = []
    unknown: list[str] = []
    name = company.name.lower()
    text = " ".join(filter(None, [company.name, company.tagline, company.about, company.industry])).lower()
    for ex in cs.exclude_companies:
        e = ex.strip().lower().strip("/").split("/")[-1]
        if e and (e == (company.slug or "").lower() or e == name):
            reasons.append(f"excluded company {ex!r}")
    for kw in cs.exclude_keywords:
        if kw.lower() in text:
            reasons.append(f"excluded keyword {kw!r}")
    if cs.employee_size:
        if company.size_bucket:
            if company.size_bucket not in cs.employee_size:
                reasons.append(f"size {company.size_bucket} not in {cs.employee_size}")
        else:
            unknown.append("employee_size")
    im = industry_matches(company, cs.industries)
    if im is False:
        reasons.append(f"industry {company.industry!r} does not match {cs.industries}")
    elif im is None and cs.industries:
        unknown.append("industry")
    if cs.locations:
        hm = hq_matches(company.hq, cs.locations)
        if hm is False:
            reasons.append(f"HQ {company.hq!r} not in {cs.locations}")
        elif hm is None:
            unknown.append("hq")
    fy = cs.founded_year
    if fy.min or fy.max:
        if company.founded:
            if (fy.min and company.founded < fy.min) or (fy.max and company.founded > fy.max):
                reasons.append(f"founded {company.founded} outside {fy.min}-{fy.max}")
        else:
            unknown.append("founded")
    if cs.company_types:
        if company.company_type:
            if not any(t.lower() in company.company_type.lower() for t in cs.company_types):
                reasons.append(f"type {company.company_type!r} not in {cs.company_types}")
        else:
            unknown.append("company_type")
    return (not reasons), reasons, unknown
