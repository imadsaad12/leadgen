"""Run events: persisted to SQLite and fanned out to in-process subscribers (SSE, CLI progress)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from leadgen.models import Event
from leadgen.store.db import Database, dumps

log = logging.getLogger("leadgen.events")


class EventBus:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._subscribers: dict[str | None, set[asyncio.Queue[Event | None]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, run_id: str | None, level: str, msg: str, data: dict[str, Any] | None = None) -> Event:
        ev = Event(run_id=run_id, ts=datetime.now(UTC), level=level, msg=msg, data=data)  # type: ignore[arg-type]
        self.db.execute(
            "INSERT INTO events(run_id, ts, level, msg, data_json) VALUES(?,?,?,?,?)",
            (run_id, ev.ts.isoformat(timespec="seconds"), level, msg, dumps(data) if data else None),
        )
        getattr(log, level if level != "warning" else "warning")("[%s] %s", run_id or "-", msg)
        targets = self._subscribers.get(run_id, set()) | self._subscribers.get(None, set())
        for q in list(targets):
            self._put(q, ev)
        return ev

    def _put(self, q: asyncio.Queue[Event | None], ev: Event | None) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(q.put_nowait, ev)
        else:
            q.put_nowait(ev)

    def info(self, run_id: str | None, msg: str, **data: Any) -> Event:
        return self.emit(run_id, "info", msg, data or None)

    def warning(self, run_id: str | None, msg: str, **data: Any) -> Event:
        return self.emit(run_id, "warning", msg, data or None)

    def error(self, run_id: str | None, msg: str, **data: Any) -> Event:
        return self.emit(run_id, "error", msg, data or None)

    def debug(self, run_id: str | None, msg: str, **data: Any) -> Event:
        return self.emit(run_id, "debug", msg, data or None)

    def subscribe(self, run_id: str | None = None) -> asyncio.Queue[Event | None]:
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str | None, q: asyncio.Queue[Event | None]) -> None:
        self._subscribers.get(run_id, set()).discard(q)

    def close_run(self, run_id: str) -> None:
        for q in list(self._subscribers.get(run_id, set())):
            self._put(q, None)

    def history(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, ts, level, msg, data_json FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, after_id, limit),
        )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "level": r["level"],
                "msg": r["msg"],
                "data": json.loads(r["data_json"]) if r["data_json"] else None,
            }
            for r in rows
        ]
