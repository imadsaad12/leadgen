"""Google Maps backend for the local-business source, via the self-hosted gosom/google-maps-scraper.

No Google API key: the app runs the scraper's Docker image for one job (CLI mode), reads its JSON output, and
maps it onto the same Business shape as the OSM backend. This is scraping Google, so keep volume modest; there is
no account to ban, only an IP to throttle.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from leadgen.sources.osm import Business

# Map our category groups to Google Maps search terms (kept small so a job stays quick).
CATEGORY_QUERIES: dict[str, list[str]] = {
    "healthcare": ["pharmacies", "clinics", "dental clinics", "hospitals"],
    "retail": ["shops", "supermarkets", "electronics stores"],
    "food": ["restaurants", "cafes"],
    "finance": ["banks", "insurance companies", "money exchange"],
    "realestate": ["real estate agencies"],
    "automotive": ["car rental", "car dealerships"],
    "hospitality": ["hotels"],
    "fitness": ["gyms"],
    "education": ["schools", "language schools"],
    "services": ["travel agencies", "marketing agencies", "logistics companies"],
}

# Obvious placeholder/noise emails the scraper picks up from site templates.
_EMAIL_BAD = re.compile(
    r"(example\.(com|org)|domain\.com|yourdomain|john@doe|name@|hello@example|test@|no-?reply@|"
    r"sentry\.|wixpress|godaddy|@email\.com|user@|someone@|email@)", re.I)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def build_queries(country: str, city: str | None, categories: list[str], per_category: int = 2) -> list[str]:
    place = f"{city}, {country}" if city else country
    out: list[str] = []
    for cat in categories:
        for term in CATEGORY_QUERIES.get(cat, [])[:per_category]:
            out.append(f"{term} in {place}")
    return out


def clean_emails(emails: list[str] | None) -> list[str]:
    out = []
    for e in emails or []:
        e = e.strip().lower()
        if _EMAIL_RE.match(e) and not _EMAIL_BAD.search(e):
            out.append(e)
    return sorted(set(out))


def parse_results(path: Path) -> list[Business]:
    rows: list[dict] = []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
    out: list[Business] = []
    seen: set[str] = set()
    for r in rows:
        name = r.get("title")
        if not name:
            continue
        phone = r.get("phone")
        key = f"{name}|{phone}".lower()
        if key in seen:
            continue
        seen.add(key)
        emails = clean_emails(r.get("emails"))
        out.append(Business(
            osm_id=str(r.get("place_id") or r.get("cid") or r.get("data_id") or name),
            name=name, category=(r.get("category") or (r.get("categories") or ["business"])[0] or "business"),
            phone=phone, website=r.get("web_site") or r.get("website"),
            email=emails[0] if emails else None,
            address=r.get("complete_address", {}).get("address") if isinstance(r.get("complete_address"), dict) else r.get("address"),
            lat=r.get("latitude"), lon=r.get("longitude"),
        ))
    return out


class GmapsClient:
    def __init__(self, image: str = "gosom/google-maps-scraper:latest", docker: str = "docker",
                 depth: int = 1, concurrency: int = 4, email: bool = True, inactivity: str = "2m",
                 timeout_s: float = 1200.0, cache_volume: str = "gmaps-pw-cache") -> None:
        self.image, self.docker = image, docker
        self.depth, self.concurrency, self.email = depth, concurrency, email
        self.inactivity, self.timeout_s, self.cache_volume = inactivity, timeout_s, cache_volume

    def available(self) -> tuple[bool, str]:
        try:
            subprocess.run([self.docker, "info"], capture_output=True, timeout=15, check=True)
        except Exception as e:  # noqa: BLE001
            return False, f"Docker is not running ({type(e).__name__}). Start Docker Desktop."
        return True, "ok"

    def run(self, queries: list[str], workdir: Path) -> list[Business]:
        workdir.mkdir(parents=True, exist_ok=True)
        qfile = workdir / "queries.txt"
        qfile.write_text("\n".join(queries) + "\n", encoding="utf-8")
        out = workdir / "out"
        out.mkdir(exist_ok=True)
        args = [self.docker, "run", "--rm",
                "-v", f"{self.cache_volume}:/opt",
                "-v", f"{qfile}:/queries.txt:ro",
                "-v", f"{out}:/out",
                self.image,
                "-input", "/queries.txt", "-results", "/out/results.json", "-json",
                "-depth", str(self.depth), "-c", str(self.concurrency),
                "-exit-on-inactivity", self.inactivity]
        if self.email:
            args.append("-email")
        subprocess.run(args, capture_output=True, timeout=self.timeout_s)
        res = out / "results.json"
        return parse_results(res) if res.exists() else []

    def businesses(self, country: str, city: str | None, categories: list[str], limit: int = 200) -> list[Business]:
        queries = build_queries(country, city, categories)
        with tempfile.TemporaryDirectory(prefix="gmaps-") as td:
            biz = self.run(queries, Path(td))
        for b in biz:
            b.country, b.city = country, (b.city or city)
        return biz[:limit]
