"""Rate limiting and budgets for LinkedIn page loads.

Every uncached tool call goes through ``Budget.acquire()`` which enforces: a jittered delay since the
previous call, a longer break every N calls, a daily page-load budget, a monthly search cap, an optional
run-hours window, and a circuit breaker tripped by challenge pages / rate-limit sentinels.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Protocol

from leadgen.models import RunSpec
from leadgen.store.db import Database


class BudgetExhausted(RuntimeError):
    """Raised when the daily or monthly budget is used up; the run pauses and can resume later."""


class CircuitOpen(RuntimeError):
    """Raised after repeated challenge / rate-limit signals; the run pauses for the day."""


class _Clock(Protocol):
    def __call__(self) -> float: ...


SEARCH_TOOLS = {"search_people", "search_companies", "search_jobs"}


class Budget:
    def __init__(self, db: Database, spec: RunSpec, *, clock: _Clock = time.monotonic, sleep=asyncio.sleep) -> None:
        self.db = db
        self.spec = spec
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None
        self._calls_since_break = 0
        self.strikes = 0
        self.max_strikes = 3
        self.session_page_loads = 0

    # -- counters ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _month() -> str:
        return datetime.now().strftime("%Y-%m")

    def today_page_loads(self) -> int:
        return int(self.db.scalar("SELECT page_loads FROM budget_days WHERE day=?", (self._today(),), 0))

    def month_searches(self) -> int:
        return int(self.db.scalar("SELECT searches FROM budget_months WHERE month=?", (self._month(),), 0))

    def record(self, tool: str, n: int = 1) -> None:
        self.db.execute(
            "INSERT INTO budget_days(day, page_loads) VALUES(?, ?) "
            "ON CONFLICT(day) DO UPDATE SET page_loads = page_loads + excluded.page_loads",
            (self._today(), n),
        )
        if tool in SEARCH_TOOLS:
            self.db.execute(
                "INSERT INTO budget_months(month, searches) VALUES(?, ?) "
                "ON CONFLICT(month) DO UPDATE SET searches = searches + excluded.searches",
                (self._month(), n),
            )
        self.session_page_loads += n

    def remaining_today(self) -> int:
        return max(0, self.spec.daily_budget - self.today_page_loads())

    def remaining_month(self) -> int:
        return max(0, self.spec.monthly_search_cap - self.month_searches())

    def summary(self) -> dict[str, int]:
        return {
            "today_page_loads": self.today_page_loads(),
            "daily_budget": self.spec.daily_budget,
            "month_searches": self.month_searches(),
            "monthly_search_cap": self.spec.monthly_search_cap,
            "session_page_loads": self.session_page_loads,
            "strikes": self.strikes,
        }

    # -- gating --------------------------------------------------------------------
    def check(self, tool: str) -> None:
        if self.strikes >= self.max_strikes:
            raise CircuitOpen("LinkedIn signalled rate limiting or a challenge repeatedly; pausing the run.")
        if self.today_page_loads() >= self.spec.daily_budget:
            raise BudgetExhausted(
                f"Daily budget of {self.spec.daily_budget} page loads reached; resume tomorrow or raise the budget."
            )
        if tool in SEARCH_TOOLS and self.month_searches() >= self.spec.monthly_search_cap:
            raise BudgetExhausted(
                f"Monthly search cap of {self.spec.monthly_search_cap} reached; LinkedIn's commercial-use limit "
                "is close. Resume next month or raise the cap at your own risk."
            )

    def _within_run_hours(self) -> bool:
        if not self.spec.run_hours:
            return True
        start, end = self.spec.run_hours
        now = datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # window crosses midnight

    async def acquire(self, tool: str) -> None:
        """Block until a new uncached call may be made, then account for it."""
        self.check(tool)
        while not self._within_run_hours():
            await self._sleep(60)
        if self._last_call is not None:
            wait = random.uniform(self.spec.min_delay_s, self.spec.max_delay_s)
            elapsed = self._clock() - self._last_call
            if elapsed < wait:
                await self._sleep(wait - elapsed)
        if self._calls_since_break >= self.spec.break_every:
            await self._sleep(random.uniform(self.spec.break_min_s, self.spec.break_max_s))
            self._calls_since_break = 0
        self._last_call = self._clock()
        self._calls_since_break += 1
        self.record(tool)

    # -- circuit breaker -----------------------------------------------------------
    def strike(self, reason: str) -> None:
        self.strikes += 1

    def clear_strikes(self) -> None:
        self.strikes = 0
