"""Raw tool-response cache keyed by (tool, canonical args). Cache hits cost no LinkedIn budget."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from leadgen.store.db import Database, dumps, utcnow

# Time-to-live per tool family. Search pages change often; profiles rarely.
DEFAULT_TTL: dict[str, timedelta] = {
    "search_people": timedelta(days=7),
    "search_companies": timedelta(days=7),
    "search_jobs": timedelta(days=3),
    "get_company_employees": timedelta(days=7),
    "get_company_profile": timedelta(days=30),
    "get_person_profile": timedelta(days=30),
    "get_my_profile": timedelta(days=1),
}


def canonical_key(tool: str, args: dict[str, Any], namespace: str = "linkedin") -> str:
    clean = {k: v for k, v in sorted(args.items()) if v not in (None, "", [], {})}
    payload = json.dumps([namespace, tool, clean], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ToolCache:
    def __init__(self, db: Database, namespace: str = "linkedin") -> None:
        self.db = db
        self.namespace = namespace
        self.hits = 0
        self.misses = 0

    def get(self, tool: str, args: dict[str, Any], *, ignore_ttl: bool = False) -> dict[str, Any] | None:
        key = canonical_key(tool, args, self.namespace)
        row = self.db.one("SELECT response_json, created_at FROM tool_cache WHERE key=?", (key,))
        if row is None:
            self.misses += 1
            return None
        if not ignore_ttl:
            ttl = DEFAULT_TTL.get(tool, timedelta(days=7))
            created = datetime.fromisoformat(row["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if datetime.now(UTC) - created > ttl:
                self.misses += 1
                return None
        self.hits += 1
        return json.loads(row["response_json"])

    def put(self, tool: str, args: dict[str, Any], response: dict[str, Any], parser_version: int = 0) -> None:
        key = canonical_key(tool, args, self.namespace)
        self.db.execute(
            """INSERT INTO tool_cache(key, tool, args_json, response_json, created_at, parser_version)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET response_json=excluded.response_json,
                   created_at=excluded.created_at, parser_version=excluded.parser_version""",
            (key, tool, dumps(args), dumps(response), utcnow(), parser_version),
        )

    def stats(self) -> dict[str, Any]:
        rows = self.db.query("SELECT tool, COUNT(*) AS n FROM tool_cache GROUP BY tool ORDER BY n DESC")
        return {"by_tool": {r["tool"]: r["n"] for r in rows}, "total": sum(r["n"] for r in rows)}

    def purge(self, older_than: timedelta | None = None, tool: str | None = None) -> int:
        sql = "DELETE FROM tool_cache WHERE 1=1"
        params: list[Any] = []
        if older_than is not None:
            sql += " AND created_at < ?"
            params.append((datetime.now(UTC) - older_than).isoformat(timespec="seconds"))
        if tool:
            sql += " AND tool=?"
            params.append(tool)
        return self.db.execute(sql, params).rowcount

    def entries_for(self, tool: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """All cached (args, response) pairs for a tool, used to re-parse without new page loads."""
        rows = self.db.query("SELECT args_json, response_json FROM tool_cache WHERE tool=?", (tool,))
        return [(json.loads(r["args_json"]), json.loads(r["response_json"])) for r in rows]
