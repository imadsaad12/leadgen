"""OpenStreetMap backend for the local-business lead source.

Free, no key, no login: geocode a city with Nominatim, then query Overpass for businesses (with a phone) in
that area, filtered to CPaaS-relevant categories. Falls back across Overpass mirrors.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

_UA = "LeadGen/0.1 (local B2B lead-gen tool)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# CPaaS-relevant business categories -> the OSM tag filters that select them.
# Each filter is an (key, regex-of-values) pair applied to nodes/ways with a phone.
CATEGORY_GROUPS: dict[str, list[tuple[str, str]]] = {
    "healthcare": [("amenity", "clinic|hospital|pharmacy|doctors|dentist|veterinary"),
                   ("healthcare", "clinic|hospital|pharmacy|doctor|dentist|physiotherapist|laboratory")],
    "retail": [("shop", "supermarket|convenience|clothes|electronics|mobile_phone|furniture|jewelry|shoes|"
                        "cosmetics|beauty|optician|department_store|car|car_parts|hardware|appliance")],
    "food": [("amenity", "restaurant|cafe|fast_food|bar|food_court")],
    "finance": [("amenity", "bank|bureau_de_change"), ("office", "insurance|financial|accountant|tax_advisor")],
    "realestate": [("office", "estate_agent|property_management")],
    "automotive": [("shop", "car|car_repair|car_parts|tyres"), ("amenity", "car_rental|fuel|driving_school")],
    "hospitality": [("tourism", "hotel|guest_house|hostel|resort"), ("amenity", "events_venue")],
    "fitness": [("leisure", "fitness_centre|sports_centre"), ("amenity", "gym")],
    "education": [("amenity", "school|language_school|college|university|kindergarten"), ("office", "educational_institution")],
    "services": [("office", "company|it|telecommunication|logistics|travel_agent|lawyer|marketing|consulting|courier"),
                 ("shop", "travel_agency|laundry")],
}
DEFAULT_CATEGORIES = ["healthcare", "retail", "food", "finance", "realestate", "automotive", "hospitality", "services"]


@dataclass
class Business:
    osm_id: str
    name: str
    category: str
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def maps_url(self) -> str:
        if self.lat and self.lon:
            return f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}#map=18/{self.lat}/{self.lon}"
        return "https://www.openstreetmap.org/"


@dataclass
class OSMClient:
    timeout: float = 70.0
    _geo_cache: dict[str, tuple[float, float, float, float] | None] = field(default_factory=dict)

    def geocode_bbox(self, place: str) -> tuple[float, float, float, float] | None:
        """Return (south, west, north, east) for a place name, via Nominatim. None if not found."""
        if place in self._geo_cache:
            return self._geo_cache[place]
        try:
            r = httpx.get(NOMINATIM, params={"q": place, "format": "json", "limit": 1},
                          headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30, follow_redirects=True)
            data = r.json() if "json" in (r.headers.get("content-type") or "") else []
        except Exception:
            data = []
        box = None
        if data:
            bb = data[0].get("boundingbox")  # [south, north, west, east] as strings
            if bb and len(bb) == 4:
                s, n, w, e = (float(x) for x in bb)
                box = (s, w, n, e)
        self._geo_cache[place] = box
        time.sleep(1.0)  # Nominatim usage policy: <=1 req/s
        return box

    @staticmethod
    def build_query(bbox: tuple[float, float, float, float], categories: list[str], limit: int = 200) -> str:
        s, w, n, e = bbox
        bb = f"({s},{w},{n},{e})"
        clauses = []
        for cat in categories:
            for key, vals in CATEGORY_GROUPS.get(cat, []):
                # plain, indexed tag filters are fast; node+way only (relations are rare and heavy)
                for kind in ("node", "way"):
                    clauses.append(f'{kind}{bb}["{key}"~"^({vals})$"]["name"]["phone"];')
                    clauses.append(f'{kind}{bb}["{key}"~"^({vals})$"]["name"]["contact:phone"];')
        body = "\n".join(clauses)
        return f"[out:json][timeout:25];\n(\n{body}\n);\nout center {limit};"

    def overpass(self, query: str) -> list[dict[str, Any]]:
        headers = {"User-Agent": _UA, "Accept": "application/json", "Accept-Encoding": "gzip, deflate"}
        errors = []
        for ep in OVERPASS_MIRRORS:
            for method in ("POST", "GET"):
                try:
                    if method == "POST":
                        r = httpx.post(ep, data={"data": query}, headers=headers, timeout=self.timeout, follow_redirects=True)
                    else:
                        r = httpx.get(ep, params={"data": query}, headers=headers, timeout=self.timeout, follow_redirects=True)
                    if "json" in (r.headers.get("content-type") or ""):
                        return r.json().get("elements", [])
                    errors.append(f"{ep.split('/')[2]} {method} {r.status_code}")
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{ep.split('/')[2]} {method} {type(ex).__name__}")
        raise RuntimeError("all Overpass mirrors failed (" + "; ".join(errors[:6]) + ")")

    @staticmethod
    def parse(elements: list[dict[str, Any]]) -> list[Business]:
        out: list[Business] = []
        seen: set[str] = set()
        for el in elements:
            t = el.get("tags", {})
            name = t.get("name:en") or t.get("name")
            if not name:
                continue
            cat = (t.get("shop") or t.get("amenity") or t.get("office") or t.get("healthcare")
                   or t.get("tourism") or t.get("leisure") or "business")
            phone = t.get("phone") or t.get("contact:phone")
            key = f"{name}|{phone}".lower()
            if key in seen:
                continue
            seen.add(key)
            center = el.get("center") or {}
            addr_parts = [t.get("addr:street"), t.get("addr:housenumber"), t.get("addr:city")]
            out.append(Business(
                osm_id=f"{el.get('type','node')}/{el.get('id')}",
                name=name, category=cat.replace("_", " "),
                phone=phone, whatsapp=t.get("contact:whatsapp") or t.get("whatsapp"),
                website=t.get("website") or t.get("contact:website"),
                email=t.get("email") or t.get("contact:email"),
                address=", ".join(p for p in addr_parts if p) or None,
                city=t.get("addr:city"),
                lat=el.get("lat") or center.get("lat"), lon=el.get("lon") or center.get("lon"),
            ))
        return out

    def businesses(self, country: str, city: str | None, categories: list[str], limit: int = 200) -> list[Business]:
        place = f"{city}, {country}" if city else country
        bbox = self.geocode_bbox(place)
        if bbox is None:
            raise RuntimeError(f"could not geocode {place!r}")
        query = self.build_query(bbox, categories or DEFAULT_CATEGORIES, limit=limit)
        biz = self.parse(self.overpass(query))
        for b in biz:
            b.country, b.city = country, (b.city or city)
        return biz
