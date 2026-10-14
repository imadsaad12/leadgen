#!/usr/bin/env python
"""Record one raw tool response from the running LinkedIn sidecar as a scrubbed test fixture.

Usage:
  uv run python scripts/record_fixture.py search_people '{"keywords":"engineer","current_company":"1441"}' people_search_1
  uv run python scripts/record_fixture.py get_company_profile '{"company_name":"docker"}' company_about_docker

Names and usernames are replaced with deterministic fakes so no real personal data lands in the repo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

from leadgen.config import get_settings
from leadgen.extract.common import flatten_references, person_username
from leadgen.sources.linkedin_mcp import LinkedInMCP

FIRST = ["Alex", "Sam", "Noor", "Lee", "Dana", "Rami", "Kim", "Jo", "Ava", "Max", "Mia", "Ola", "Tariq", "Lina", "Omar"]
LAST = ["Nasser", "Berg", "Rossi", "Khan", "Silva", "Haddad", "Novak", "Meyer", "Costa", "Diallo", "Kato", "Weber"]


def fake_name(seed: str) -> tuple[str, str]:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    first, last = FIRST[h % len(FIRST)], LAST[(h // 7) % len(LAST)]
    return f"{first} {last}", f"{first.lower()}-{last.lower()}-{h % 9000 + 1000}"


def scrub(payload: dict) -> dict:
    text_map: dict[str, str] = {}
    refs = payload.get("references") or {}
    for _section, items in list(refs.items()) if isinstance(refs, dict) else []:
        for ref in items:
            if ref.get("kind") == "person" or (ref.get("url") or "").startswith("/in/"):
                user = person_username(ref.get("url"))
                if not user or user.startswith("ACoAA"):
                    continue
                name, fake_user = fake_name(user)
                if ref.get("text"):
                    text_map[ref["text"]] = name
                ref["url"] = f"/in/{fake_user}/"
                ref["text"] = name if ref.get("text") else ref.get("text")
                text_map[user] = fake_user
    sections = payload.get("sections") or {}
    for key, text in sections.items():
        for real, fake in sorted(text_map.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(real, fake)
        text = re.sub(r"\+?\d[\d \-()]{7,}\d", "[phone]", text)
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
        sections[key] = text
    payload["url"] = re.sub(r"/in/[^/]+/", "/in/scrubbed/", payload.get("url", ""))
    return payload


async def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    tool, args_json, name = sys.argv[1], sys.argv[2], sys.argv[3]
    settings = get_settings()
    async with LinkedInMCP(settings.mcp_url, timeout_s=180) as mcp:
        payload = await mcp.call(tool, json.loads(args_json), use_cache=False)
    out = Path("tests/fixtures/linkedin") / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = scrub(payload)
    out.write_text(json.dumps({"tool": tool, "args": json.loads(args_json), "payload": scrubbed}, indent=2, ensure_ascii=False))
    n_refs = len(flatten_references(scrubbed.get("references")))
    print(f"wrote {out} ({n_refs} references, sections={list((scrubbed.get('sections') or {}).keys())})")


if __name__ == "__main__":
    asyncio.run(main())
