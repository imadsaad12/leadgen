"""SQLite database: schema, connection and small query helpers.

One connection shared by the web handlers (threadpool) and the job runner (event loop),
serialised by a re-entrant lock. WAL mode keeps readers unblocked while a run writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_cache (
    key            TEXT PRIMARY KEY,
    tool           TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    response_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    parser_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tool_cache_tool ON tool_cache(tool, created_at);

CREATE TABLE IF NOT EXISTS llm_cache (
    key           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    response_json TEXT NOT NULL,
    usage_json    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    spec_json  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    spec_id     TEXT,
    name        TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    stats_json  TEXT NOT NULL DEFAULT '{}',
    cursor_json TEXT NOT NULL DEFAULT '{}',
    message     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_companies (
    run_id       TEXT NOT NULL,
    company_key  TEXT NOT NULL,
    status       TEXT NOT NULL,
    company_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, company_key)
);

CREATE TABLE IF NOT EXISTS run_facets (
    run_id      TEXT NOT NULL,
    company_key TEXT NOT NULL,
    facet_id    TEXT NOT NULL,
    last_page   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    new_found   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, company_key, facet_id)
);

CREATE TABLE IF NOT EXISTS run_people (
    run_id              TEXT NOT NULL,
    person_key          TEXT NOT NULL,
    company_key         TEXT NOT NULL,
    person_json         TEXT NOT NULL,
    classification_json TEXT,
    department          TEXT,
    seniority           TEXT,
    raw_title           TEXT,
    needs_review        INTEGER NOT NULL DEFAULT 0,
    relevant            INTEGER,
    fit_score           INTEGER,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, person_key)
);
CREATE INDEX IF NOT EXISTS ix_run_people_group ON run_people(run_id, department, seniority);
CREATE INDEX IF NOT EXISTS ix_run_people_company ON run_people(run_id, company_key);

CREATE TABLE IF NOT EXISTS title_cache (
    normalized_title    TEXT PRIMARY KEY,
    classification_json TEXT NOT NULL,
    method              TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_days (
    day        TEXT PRIMARY KEY,
    page_loads INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_months (
    month    TEXT PRIMARY KEY,
    searches INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,
    msg       TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(run_people)").fetchall()}
        for col in ("relevant", "fit_score"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE run_people ADD COLUMN {col} INTEGER")

    # -- low level -----------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    class _Tx:
        def __init__(self, db: Database) -> None:
            self.db = db

        def __enter__(self) -> Database:
            self.db._lock.acquire()
            self.db._conn.execute("BEGIN")
            return self.db

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.db._conn.execute("COMMIT")
                else:
                    self.db._conn.execute("ROLLBACK")
            finally:
                self.db._lock.release()

    def transaction(self) -> _Tx:
        return Database._Tx(self)

    # -- settings ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    def all_settings(self) -> dict[str, Any]:
        return {r["key"]: self.get_setting(r["key"]) for r in self.query("SELECT key FROM settings")}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
