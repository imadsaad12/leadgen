"""Country matching for company HQ strings, using the gazetteer in leadgen/data/locations.yaml.

Spec locations are countries. A LinkedIn HQ line is like "Berlin, Berlin", "Dubai, Dubai",
"New York, New York" or "144 Townsend St, San Francisco, California, US". Matching rules, chosen to avoid
cross-country collisions (e.g. UK city "York" inside US "New York"):

- Country NAMES and long aliases match as whole words anywhere in the HQ (``germany`` is unambiguous).
- Regions and CITIES match only as a whole comma-separated part of the HQ ("york" never matches "new york").
- Tokens of 2 characters or fewer (ISO-2 codes, US state abbreviations) are never matched in free text.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from leadgen.config import DATA_DIR


@lru_cache(maxsize=1)
def _gazetteer() -> dict:
    path = Path(DATA_DIR) / "locations.yaml"
    if not path.exists():
        return {"countries": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"countries": data.get("countries") or []}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _parts(hq: str) -> set[str]:
    return {p.strip() for p in re.split(r",|/|\||·|-", _norm(hq))} | {_norm(hq)}


@lru_cache(maxsize=1)
def country_names() -> list[str]:
    return sorted(c.get("name", "") for c in _gazetteer()["countries"] if c.get("name"))


@lru_cache(maxsize=1)
def _country_index() -> list[tuple[dict, set[str], set[str]]]:
    """Each country with (word-tokens matched anywhere, place-tokens matched as a whole part)."""
    out = []
    for c in _gazetteer()["countries"]:
        words = {_norm(c.get("name", ""))} | {_norm(a) for a in c.get("aliases", [])}
        words = {w for w in words if len(w) > 2}
        places = {_norm(x) for x in c.get("regions", [])} | {_norm(x) for x in c.get("cities", [])}
        places = {p for p in places if len(p) > 2}
        out.append((c, words, places))
    return out


def find_country(term: str) -> dict | None:
    t = _norm(term)
    if not t:
        return None
    for c, words, _ in _country_index():
        if t == _norm(c.get("name", "")) or t in words or t == (c.get("iso2") or "").lower():
            return c
    return None


def canonical_country(term: str) -> str | None:
    c = find_country(term)
    return c.get("name") if c else None


def _word_hit(token: str, hq: str) -> bool:
    return bool(re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", hq))


def _hits_country(hq_norm: str, parts: set[str], words: set[str], places: set[str]) -> bool:
    if any(w in parts or _word_hit(w, hq_norm) for w in words):
        return True
    return any(pl in parts for pl in places)  # cities/regions: whole comma-part only


def location_country(hq: str | None) -> str | None:
    """The country an HQ string resolves to, or None when it cannot be resolved (never guess)."""
    if not hq:
        return None
    hq_norm, parts = _norm(hq), _parts(hq)
    for c, words, places in _country_index():
        if _hits_country(hq_norm, parts, words, places):
            return c.get("name")
    return None


def expand_location(term: str) -> set[str]:
    """All tokens that, in an HQ line, imply the location is inside ``term`` (country or literal place)."""
    out = {_norm(term)}
    c = find_country(term)
    if c:
        out |= {_norm(c.get("name", ""))} | {_norm(a) for a in c.get("aliases", [])}
        out |= {_norm(x) for x in c.get("regions", [])} | {_norm(x) for x in c.get("cities", [])}
    return {t for t in out if t}


def hq_matches(hq: str | None, countries: list[str]) -> bool | None:
    """True/False when decidable, None when the HQ is unknown/empty."""
    if not hq:
        return None
    hq_norm, parts = _norm(hq), _parts(hq)
    for term in countries:
        c = find_country(term)
        if c:
            _, words, places = next((e for e in _country_index() if e[0] is c))
            if _hits_country(hq_norm, parts, words, places):
                return True
        else:
            t = _norm(term)  # literal place (e.g. a city/region the user typed)
            if len(t) > 2 and (t in parts or _word_hit(t, hq_norm)):
                return True
    return False


@lru_cache(maxsize=1)
def _geo_urns() -> dict:
    path = Path(DATA_DIR) / "geo_urns.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k.upper(): str(v) for k, v in (data.get("geo_urns") or {}).items()}


def geo_urn(term: str) -> str | None:
    """LinkedIn geo id for a country term (name, alias or ISO-2), or None if we don't have one."""
    c = find_country(term)
    iso = (c or {}).get("iso2", "").upper() if c else _norm(term).upper()
    return _geo_urns().get(iso)
