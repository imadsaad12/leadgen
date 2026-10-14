"""Source adapter contract and the per-run context handed to adapters and pipeline stages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from leadgen.models import Company, Person, RunStats, Spec

if TYPE_CHECKING:
    from leadgen.llm.claude_cli import ClaudeCLI
    from leadgen.store.budget import Budget
    from leadgen.store.cache import ToolCache
    from leadgen.store.events import EventBus
    from leadgen.store.runs import RunStore


class RunCancelled(Exception):
    """Raised inside the pipeline when the user asked to pause or stop the run."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class RunContext:
    run_id: str
    spec: Spec
    store: RunStore
    cache: ToolCache
    events: EventBus
    budget: Budget | None = None
    llm: ClaudeCLI | None = None
    stats: RunStats = field(default_factory=RunStats)
    pause_requested: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    dry_run: bool = False
    planned_calls: list[tuple[str, dict]] = field(default_factory=list)

    def check_cancelled(self) -> None:
        if self.stop_requested.is_set():
            raise RunCancelled("stopped")
        if self.pause_requested.is_set():
            raise RunCancelled("paused")

    def log(self, msg: str, level: str = "info", **data) -> None:
        self.events.emit(self.run_id, level, msg, data or None)


class Capabilities(Protocol):
    name: str
    native_company_filters: frozenset[str]
    native_people_filters: frozenset[str]


class SourceAdapter(Protocol):
    name: str

    def discover_companies(self, ctx: RunContext) -> AsyncIterator[Company]: ...

    async def profile_company(self, company: Company, ctx: RunContext) -> Company: ...

    def enumerate_people(self, company: Company, ctx: RunContext, known: set[str]) -> AsyncIterator[Person]: ...

    async def enrich_person(self, person: Person, ctx: RunContext) -> Person: ...

    async def close(self) -> None: ...
