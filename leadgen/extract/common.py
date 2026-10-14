"""Shared helpers for turning the LinkedIn server's raw text + references into records."""

from __future__ import annotations

import re
from typing import Any

PARSER_VERSION = 1

PERSON_PATH_RE = re.compile(r"^/in/([^/?#]+)/?")
COMPANY_PATH_RE = re.compile(r"^/company/([^/?#]+)/?")
ANON_RE = re.compile(r"^(ACoAA|ACw)", re.I)
DEGREE_RE = re.compile(r"(?:^|\s|•|·)\s*(1st|2nd|3rd\+?|1\.|2\.|3\.)\s*(?:degree)?\s*(?:connection)?\s*$", re.I)
NOISE_LINE_RE = re.compile(
    r"^(view .*profile|connect|message|follow|following|pending|premium|see all|show all|save|unfollow|"
    r"\d+ mutual connections?|[a-z ]+ (is|are) a mutual connection|mutual connection|"
    r"actively hiring|open to work|hiring|verified|linkedin member|current:|past:|summary:|"
    r"skills:|education:|about|more|•|·|\d+ (followers?|results?)|about \d+ results|showing \d+ results?|"
    r"people|jobs|posts|companies|groups|events|courses|schools|services|products|all filters|"
    r"locations|current company|connections|reset|apply|show results|1st|2nd|3rd\+?|)$",
    re.I,
)
FOLLOWERS_RE = re.compile(r"^\s*([\d.,]+)\s*([km])?\s*followers?\s*$", re.I)
EMPLOYEES_RE = re.compile(r"(?:see all\s+)?([\d.,]+)\s*(?:\+)?\s*(?:employees?|associated members?|people)\b", re.I)
SIZE_RE = re.compile(r"\b(1-10|11-50|51-200|201-500|501-1,?000|1,?001-5,?000|5,?001-10,?000|10,?001\+)\s*employees?\b", re.I)
LOCATION_HINT_RE = re.compile(
    r"(,|\bArea\b|\bGreater\b|\bRegion\b|\bMetropolitan\b|\bRemote\b|\bUnited\b|\bKingdom\b|\bGermany\b|"
    r"\bFrance\b|\bSpain\b|\bItaly\b|\bNetherlands\b|\bCanada\b|\bAustralia\b|\bIndia\b|\bEmirates\b|"
    r"\bSaudi\b|\bEgypt\b|\bBrazil\b|\bMexico\b|\bSingapore\b|\bJapan\b|\bChina\b|\bPoland\b|\bSweden\b|"
    r"\bSwitzerland\b|\bAustria\b|\bBelgium\b|\bIreland\b|\bPortugal\b|\bTurkey\b|\bTürkiye\b|\bLebanon\b|"
    r"\bJordan\b|\bQatar\b|\bKuwait\b|\bMorocco\b|\bNigeria\b|\bKenya\b|\bSouth Africa\b|\bIsrael\b|\bPakistan\b)",
    re.I,
)
TITLE_WORD_RE = re.compile(
    r"\b(manager|engineer|director|head|chief|officer|lead|senior|junior|specialist|analyst|consultant|developer|"
    r"designer|founder|ceo|cto|coo|cfo|cmo|vp|president|sales|marketing|product|owner|partner|assistant|intern|"
    r"student|recruiter|account|executive|architect|scientist|advisor|coordinator|associate|representative|"
    r"strategist|freelance|entrepreneur|investor|teacher|nurse|doctor|lawyer|counsel|trainer|coach|writer|editor)\b",
    re.I,
)
TITLE_AT_RE = re.compile(r"^(?P<title>.+?)\s+(?:at|@|bei|chez|en|presso)\s+(?P<company>.+)$", re.I)
TITLE_SEP_RE = re.compile(r"\s*(?:\||•|·|–|—| - )\s*")


def flatten_references(refs: Any) -> list[dict[str, Any]]:
    """references may be {section: [ref, ...]} or [ref, ...]; return a flat ordered list of dicts."""
    out: list[dict[str, Any]] = []
    if isinstance(refs, dict):
        for section in refs.values():
            out.extend(flatten_references(section))
    elif isinstance(refs, list):
        for r in refs:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, str):
                out.append({"kind": "person" if r.startswith("/in/") else "company" if r.startswith("/company/") else "external", "url": r})
    return out


def person_username(url: str | None) -> str | None:
    if not url:
        return None
    path = url
    if path.startswith("http"):
        m = re.search(r"linkedin\.com(/in/[^/?#]+)", path)
        path = m.group(1) if m else path
    m = PERSON_PATH_RE.match(path)
    return m.group(1) if m else None


def company_slug(url: str | None) -> str | None:
    if not url:
        return None
    path = url
    if path.startswith("http"):
        m = re.search(r"linkedin\.com(/company/[^/?#]+)", path)
        path = m.group(1) if m else path
    m = COMPANY_PATH_RE.match(path)
    return m.group(1) if m else None


def is_anonymized(username: str | None, name: str | None = None) -> bool:
    if username and ANON_RE.match(username):
        return True
    return bool(name and name.strip().lower() in {"linkedin member", "linkedin user", "membre linkedin", "linkedin-mitglied"})


def name_from_username(username: str) -> str:
    parts = [p for p in re.split(r"[-_.]", username) if p and not re.fullmatch(r"[0-9a-f]{3,}|\d+", p)]
    return " ".join(p.capitalize() for p in parts)


def lines_of(text: str) -> list[str]:
    return [ln.strip() for ln in text.replace("\r", "").split("\n")]


def is_noise(line: str) -> bool:
    return not line or bool(NOISE_LINE_RE.match(line.strip()))


def looks_like_location(line: str) -> bool:
    if len(line) > 80 or len(line) < 3:
        return False
    if TITLE_AT_RE.match(line) or TITLE_WORD_RE.search(line):
        return False
    return bool(LOCATION_HINT_RE.search(line))


def parse_followers(line: str) -> int | None:
    m = FOLLOWERS_RE.match(line)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
    return int(n * mult)


def parse_int(text: str) -> int | None:
    m = re.search(r"[\d][\d.,]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", "").replace(".", ""))
    except ValueError:
        return None


def split_headline(headline: str) -> tuple[str | None, str | None]:
    """'Senior Engineer at Acme | Speaker' -> ('Senior Engineer', 'Acme'); 'CTO | Co-founder' -> ('CTO | Co-founder', None)."""
    if not headline:
        return None, None
    first = TITLE_SEP_RE.split(headline, maxsplit=1)[0].strip() or headline
    m = TITLE_AT_RE.match(first)
    if m:
        return m.group("title").strip(" ,"), m.group("company").strip(" ,")
    m = TITLE_AT_RE.match(headline)
    if m:
        return m.group("title").strip(" ,"), m.group("company").strip(" ,")
    return headline.strip(), None


def find_line_index(lines: list[str], needle: str, start: int = 0) -> int:
    n = needle.strip().lower()
    for i in range(start, len(lines)):
        if lines[i].lower() == n:
            return i
    for i in range(start, len(lines)):
        if n and n in lines[i].lower():
            return i
    return -1
