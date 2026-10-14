"""Deterministic parser for people-search pages and company /people/ pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from leadgen.extract.common import (
    DEGREE_RE,
    EMPLOYEES_RE,
    PARSER_VERSION,
    find_line_index,
    flatten_references,
    is_anonymized,
    is_noise,
    lines_of,
    looks_like_location,
    name_from_username,
    person_username,
    split_headline,
)
from leadgen.models import ExperienceEntry, Person

CURRENT_RE = re.compile(r"^(current|aktuell|actuel|actual)\s*:\s*(.+)$", re.I)
SECTION_HEADINGS = (
    "where they live",
    "where they studied",
    "what they do",
    "what they are skilled at",
    "how you're connected",
    "how you are connected",
)


@dataclass
class ParsedPage:
    people: list[Person]
    status: str  # ok | degraded | empty
    references: int
    cards: int
    notes: list[str] = field(default_factory=list)
    total_hint: int | None = None
    function_breakdown: dict[str, int] = field(default_factory=dict)


def _person_refs(payload: dict[str, Any]) -> list[tuple[str, str | None]]:
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for ref in flatten_references(payload.get("references")):
        if ref.get("kind") not in (None, "person"):
            continue
        username = person_username(ref.get("url"))
        if not username or username in seen:
            continue
        seen.add(username)
        out.append((username, (ref.get("text") or "").strip() or None))
    return out


def _page_text(payload: dict[str, Any]) -> str:
    sections = payload.get("sections") or {}
    for key in ("search_results", "employees"):
        if sections.get(key):
            return sections[key]
    return "\n".join(v for v in sections.values() if isinstance(v, str))


def _card_lines(lines: list[str], start: int, end: int) -> list[str]:
    return [ln for ln in lines[start:end] if ln]


def _parse_card(username: str, name: str, card: list[str], company_key: str, company_name: str | None,
                found_via: str) -> Person:
    degree: str | None = None
    headline: str | None = None
    location: str | None = None
    current: str | None = None
    for ln in card:
        if ln.lower() == name.lower():
            continue
        m = DEGREE_RE.search(ln)
        if m and len(ln) <= 40:
            degree = m.group(1).rstrip(".")
            continue
        cm = CURRENT_RE.match(ln)
        if cm:
            current = cm.group(2).strip()
            continue
        if is_noise(ln):
            continue
        if headline is None and looks_like_location(ln):
            # profile without a headline: the first content line is already the location
            location = location or ln
            continue
        if headline is None:
            headline = ln
            continue
        if location is None and looks_like_location(ln):
            location = ln
            continue
    title_source = current or headline or ""
    raw_title, listed_company = split_headline(title_source)
    return Person(
        key=username,
        username=username,
        profile_url=f"https://www.linkedin.com/in/{username}/",
        full_name=name,
        headline=headline,
        raw_title=raw_title or None,
        location=location,
        degree=degree,
        company_key=company_key,
        company_name=listed_company or company_name,
        anonymized=is_anonymized(username, name),
        found_via=[found_via],
        parse_method="heuristic",
        needs_review=not raw_title,
    )


def parse_people_page(payload: dict[str, Any], *, company_key: str, company_name: str | None = None,
                      found_via: str = "search") -> ParsedPage:
    refs = _person_refs(payload)
    text = _page_text(payload)
    lines = lines_of(text)
    notes: list[str] = []
    total_hint = None
    for ln in lines[:24]:
        m = re.search(r"([\d.,]+)\+?\s+(?:associated members?|results?)", ln, re.I) or EMPLOYEES_RE.search(ln)
        if m:
            try:
                total_hint = int(m.group(1).replace(",", "").replace(".", ""))
                break
            except ValueError:
                pass
    breakdown = _function_breakdown(lines)
    if not refs:
        return ParsedPage([], "empty" if not text.strip() else "degraded", 0, 0, ["no person references"], total_hint,
                          breakdown)

    # locate each person's anchor line in order; names come from reference text, else from the username
    anchors: list[tuple[int, str, str]] = []
    cursor = 0
    for username, ref_name in refs:
        name = ref_name or name_from_username(username)
        idx = find_line_index(lines, name, cursor) if name else -1
        if idx < 0 and ref_name is None:
            idx = -1
        anchors.append((idx, username, name))
        if idx >= 0:
            cursor = idx + 1
    located = [a for a in anchors if a[0] >= 0]
    people: list[Person] = []
    for i, (idx, username, name) in enumerate(anchors):
        if idx < 0:
            people.append(
                Person(
                    key=username, username=username, profile_url=f"https://www.linkedin.com/in/{username}/",
                    full_name=name, company_key=company_key, company_name=company_name,
                    anonymized=is_anonymized(username, name), found_via=[found_via],
                    parse_method="reference_only", needs_review=True,
                )
            )
            continue
        nxt = next((a[0] for a in anchors[i + 1:] if a[0] > idx), len(lines))
        card = _card_lines(lines, idx, min(nxt, idx + 14))
        people.append(_parse_card(username, name, card, company_key, company_name, found_via))

    cards = len(located)
    aligned = sum(1 for p in people if p.parse_method == "heuristic" and p.raw_title)
    if cards == 0:
        status = "degraded"
        notes.append("no anchors located in text")
    elif aligned / max(len(refs), 1) < 0.8:
        status = "degraded"
        notes.append(f"only {aligned}/{len(refs)} cards yielded a title")
    else:
        status = "ok"
    return ParsedPage(people, status, len(refs), cards, notes, total_hint, breakdown)


# LinkedIn renders each demographic row as "<count> <label>" (count first), e.g. "320 Engineering",
# followed by a "toggle off"/"Add" control line. Older/locale variants put the count last.
_BREAKDOWN_SKIP = {"toggle off", "toggle on", "add", "show more", "show less", ""}
_COUNT_FIRST_RE = re.compile(r"^([\d][\d,]*)\s+(.+?)\s*$")
_COUNT_LAST_RE = re.compile(r"^(.+?)\s+([\d][\d,]*)\s*$")


def _function_breakdown(lines: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    low = [ln.lower() for ln in lines]
    try:
        start = low.index("what they do")
    except ValueError:
        return out
    i = start + 1
    while i < len(lines) and i < start + 60:
        ln = lines[i].strip()
        if ln.lower() in SECTION_HEADINGS:
            break
        if ln.lower() in _BREAKDOWN_SKIP:
            i += 1
            continue
        m = _COUNT_FIRST_RE.match(ln) or _COUNT_LAST_RE.match(ln)
        if m:
            a, b = m.group(1), m.group(2)
            count, label = (a, b) if a.replace(",", "").isdigit() else (b, a)
            label = label.strip()
            if label and not label.lower() in _BREAKDOWN_SKIP:
                out[label] = int(count.replace(",", ""))
        i += 1
    return out


def parse_experience(payload: dict[str, Any]) -> list[ExperienceEntry]:
    """Loose parse of a profile's experience section into entries."""
    text = (payload.get("sections") or {}).get("experience") or ""
    entries: list[ExperienceEntry] = []
    block: list[str] = []
    date_re = re.compile(r"(\b(19|20)\d{2}\b|present|heute|aujourd|actualidad)", re.I)
    for ln in lines_of(text) + [""]:
        if ln and not is_noise(ln):
            block.append(ln)
            continue
        if block:
            e = ExperienceEntry()
            for b in block:
                if date_re.search(b) and e.dates is None and ("-" in b or "–" in b or "·" in b):
                    e.dates = b
                elif e.title is None:
                    e.title = b
                elif e.company is None:
                    e.company = b.split(" · ")[0].strip()
                elif e.location is None and looks_like_location(b):
                    e.location = b
            if e.title:
                entries.append(e)
            block = []
    return entries


__all__ = ["ParsedPage", "parse_people_page", "parse_experience", "PARSER_VERSION"]
