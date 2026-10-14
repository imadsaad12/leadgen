"""Single-worker job runner: one pipeline run at a time (the LinkedIn sidecar is sequential anyway)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from leadgen.config import Settings
from leadgen.pipeline.run import Pipeline
from leadgen.sources import make_source_factory
from leadgen.store.events import EventBus
from leadgen.store.runs import RunStore


@dataclass
class ActiveRun:
    run_id: str
    task: asyncio.Task
    pause: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class JobRunner:
    def __init__(self, settings: Settings, pipeline: Pipeline, store: RunStore, events: EventBus, manager=None) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.store = store
        self.events = events
        self.manager = manager
        self.active: ActiveRun | None = None
        self.queue: list[str] = []
        self._lock = asyncio.Lock()

    def recover(self) -> int:
        """On startup, runs left 'running' by a previous process become 'paused' (resumable)."""
        n = 0
        for r in self.store.running_runs():
            self.store.set_status(r.id, "paused", "app restarted; resume to continue")
            n += 1
        return n

    def is_active(self, run_id: str) -> bool:
        return self.active is not None and self.active.run_id == run_id and not self.active.task.done()

    async def ensure_sidecar(self, run_id: str) -> bool:
        """Bring the LinkedIn sidecar up before a run needs it. Returns False if it cannot be reached."""
        if self.manager is None:
            return True
        if await self.manager.healthy():
            return True
        self.events.info(run_id, "starting the LinkedIn sidecar...")
        try:
            msg = await self.manager.start()
        except Exception as e:  # noqa: BLE001
            self.events.error(run_id, f"could not start the LinkedIn sidecar: {e}")
            return False
        ok = await self.manager.healthy()
        self.events.info(run_id, f"LinkedIn sidecar: {msg}" + ("" if ok else " (not reachable)"))
        return ok

    async def start(self, run_id: str) -> str:
        state = self.store.get_run(run_id)
        if state is not None and state.spec.run.source == "linkedin":
            if not await self.ensure_sidecar(run_id):
                self.store.set_status(run_id, "failed",
                                      "LinkedIn sidecar is not reachable; start it from Settings and resume.")
                return "sidecar unavailable"
        async with self._lock:
            if self.active and not self.active.task.done():
                if run_id not in self.queue and self.active.run_id != run_id:
                    self.queue.append(run_id)
                    self.store.set_status(run_id, "queued")
                return "queued"
            self._launch(run_id)
            return "started"

    def _launch(self, run_id: str) -> None:
        state = self.store.get_run(run_id)
        if state is None:
            return
        pause, stop = asyncio.Event(), asyncio.Event()
        llm = None
        if state.spec.run.llm_assist is not False and self.store.db.get_setting("llm_assist", True):
            from leadgen.llm.claude_cli import build_claude_cli

            cli = build_claude_cli(self.settings, self.store, self.events, run_id)
            llm = cli if cli.available() else None
        factory = make_source_factory(state.spec.run.source, self.settings)
        task = asyncio.create_task(self.pipeline.execute(state, factory, pause=pause, stop=stop, llm=llm))
        self.active = ActiveRun(run_id, task, pause, stop)
        task.add_done_callback(lambda t: asyncio.create_task(self._next()))

    async def _next(self) -> None:
        async with self._lock:
            if self.active and not self.active.task.done():
                return
            if self.queue:
                self._launch(self.queue.pop(0))

    async def pause(self, run_id: str) -> str:
        if self.is_active(run_id):
            self.active.pause.set()
            return "pausing"
        if run_id in self.queue:
            self.queue.remove(run_id)
            self.store.set_status(run_id, "paused", "removed from queue")
            return "dequeued"
        return "not running"

    async def stop(self, run_id: str) -> str:
        if self.is_active(run_id):
            self.active.stop.set()
            return "stopping"
        if run_id in self.queue:
            self.queue.remove(run_id)
            self.store.set_status(run_id, "stopped", "removed from queue")
            return "dequeued"
        state = self.store.get_run(run_id)
        if state and state.status in ("paused", "queued"):
            self.store.set_status(run_id, "stopped", "stopped while paused")
            return "stopped"
        return "not running"

    async def resume(self, run_id: str) -> str:
        state = self.store.get_run(run_id)
        if state is None:
            return "unknown run"
        if state.status not in ("paused", "failed", "stopped", "queued"):
            return f"cannot resume a run in status {state.status}"
        return await self.start(run_id)

    async def shutdown(self) -> None:
        if self.active and not self.active.task.done():
            self.active.pause.set()
            try:
                await asyncio.wait_for(self.active.task, timeout=30)
            except (TimeoutError, asyncio.CancelledError):
                self.active.task.cancel()
