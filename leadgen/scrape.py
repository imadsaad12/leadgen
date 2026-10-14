"""On-demand scrapers exposed as explicit modes, separate from a full lead-gen run.

- ``decision_maker_spec`` builds a single-company run that returns a company's decision-makers.
- ``scrape_profile`` reads one person's full LinkedIn profile.

The company scraper reuses the normal company-first pipeline (so it gets progress, export and the relevance
filter), targeting decision-maker seniorities through the company's people search. The profile scraper is a
single page load rendered immediately.
"""

from __future__ import annotations

from typing import Any

from leadgen.extract.common import lines_of, person_username
from leadgen.extract.people import parse_experience
from functools import lru_cache
from pathlib import Path as _Path

import yaml

from leadgen.config import DATA_DIR
from leadgen.models import CompanySpec, PeopleSpec, RunSpec, Seniority, Spec

# What counts as a "decision maker" for the company scraper.
DECISION_MAKER_SENIORITIES: list[Seniority] = [Seniority.DIRECTOR, Seniority.VP, Seniority.CXO, Seniority.FOUNDER]
WITH_MANAGERS: list[Seniority] = [Seniority.MANAGER, Seniority.LEAD, *DECISION_MAKER_SENIORITIES]

import re as _re

_EMAIL_RE = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = _re.compile(r"(?<!\d)(\+?\d[\d\s().-]{6,}\d)")
_URL_RE = _re.compile(r"(?:https?://|www\.)[^\s|]+", _re.I)
_PHONE_LABEL_RE = _re.compile(r"(phone|mobile|tel|whatsapp)", _re.I)


def _extract_contacts(sections: dict) -> dict:
    """Pull email / phone / websites out of the LinkedIn 'Contact info' section, when it is visible."""
    text = sections.get("contact_info") or ""
    emails = sorted(set(_EMAIL_RE.findall(text)))
    websites = sorted({u.rstrip(".,)") for u in _URL_RE.findall(text) if "linkedin.com/in/" not in u.lower()})
    phones = []
    for line in text.splitlines():
        for m in _PHONE_RE.findall(line):
            digits = _re.sub(r"\D", "", m)
            if 7 <= len(digits) <= 15:
                phones.append(m.strip())
    phones = sorted(set(phones))
    return {"emails": emails, "phones": phones, "websites": websites,
            "available": bool(emails or phones or websites)}

PROFILE_SECTIONS = "contact_info,experience,education,skills,certifications"


@lru_cache(maxsize=4)
def role_preset(name: str = "cpaas") -> tuple[list[str], list[str], list[str]]:
    """Return (titles, exclude_titles, facets) for a decision-maker preset.

    'cpaas' = people worth contacting about a CPaaS offer. ``facets`` are pre-grouped people-search queries
    (one LinkedIn search each) so a company scrape stays gentle.
    """
    path = _Path(DATA_DIR) / f"{name}_titles.yaml"
    if not path.exists():
        return [], [], []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    titles = [t for t in (data.get("titles") or []) if t]
    excludes = [e.lower() for e in (data.get("exclude_titles") or []) if e]
    facets = [f for f in (data.get("facets") or []) if f]
    return titles, excludes, facets



def decision_maker_spec(
    company: str,
    *,
    name: str | None = None,
    max_people: int = 40,
    preset: str = "cpaas",
    offer: str = "",
    source: str = "linkedin",
) -> Spec:
    """A company-first run seeded with one company, targeting the buyer titles for a preset (default: CPaaS).

    Searches the company's people for the preset's decision-maker titles (sales, technical leaders, product, CX,
    marketing, comms, exec) and excludes individual developers/engineers and other non-buyers.
    """
    titles, excludes, facets = role_preset(preset)
    label = name or company.strip().strip("/").split("/")[-1]
    return Spec(
        name=f"decision-makers: {label}",
        offer=offer,
        company=CompanySpec(seed_companies=[company], max_companies=1),
        people=PeopleSpec(titles=titles, title_facets=facets, exclude_titles=excludes, seniorities=[],
                          broad_sweep=False, max_per_company=max_people, max_pages_per_facet=3),
        run=RunSpec(source=source, strategy="company_first"),
    )


def _first_lines(text: str, n: int = 6) -> list[str]:
    out = []
    for ln in lines_of(text):
        if ln and ln.lower() not in ("verify in 2 minutes", "contact info", "more", "message", "connect", "follow"):
            out.append(ln)
        if len(out) >= n:
            break
    return out


async def scrape_profile(mcp, linkedin: str, sections: str = PROFILE_SECTIONS, max_scrolls: int = 3) -> dict[str, Any]:
    """Read one person's profile and return a structured summary plus the raw sections."""
    username = person_username(linkedin) or linkedin.strip().strip("/").split("/")[-1]
    payload = await mcp.call(
        "get_person_profile",
        {"linkedin_username": username, "sections": sections, "max_scrolls": max_scrolls},
        use_cache=True,
    )
    secs = payload.get("sections") or {}
    main = secs.get("main_profile") or secs.get("main") or next(iter(secs.values()), "")
    header = _first_lines(main, 4)
    experience = parse_experience(payload)
    contacts = _extract_contacts(secs)
    return {
        "username": username,
        "contacts": contacts,
        "url": payload.get("url") or f"https://www.linkedin.com/in/{username}/",
        "name": header[0] if header else username,
        "headline": header[1] if len(header) > 1 else None,
        "location": next((h for h in header[1:] if "," in h or any(x in h for x in ("Area", "Region"))), None),
        "experience": [e.model_dump() for e in experience],
        "sections": {k: v for k, v in secs.items() if isinstance(v, str)},
        "section_errors": payload.get("section_errors") or {},
    }
