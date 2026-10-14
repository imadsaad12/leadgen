"""Thin, safe wrapper around the vendored linkedin-mcp-server (MCP v2 client).

Guarantees: read-only tool allowlist, one call at a time, cache-first, budget-gated, timeouts, and
classification of failures (not logged in / rate limited / challenge) so the pipeline can pause cleanly.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from mcp import Client, StdioServerParameters

from leadgen.store.budget import Budget
from leadgen.store.cache import ToolCache

ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "search_companies",
        "get_company_profile",
        "get_company_employees",
        "search_people",
        "search_jobs",
        "get_person_profile",
        "get_my_profile",
        "close_session",
    }
)

RATE_LIMITED_TEXT = "[Rate limited]"
_LOGIN_RE = re.compile(r"not logged in|log ?in|login|sign in|authentication|session (expired|invalid)|unauthenticated", re.I)
_CHALLENGE_RE = re.compile(r"captcha|security verification|challenge|unusual activity|verify (it'?s )?you|rate.?limit|too many requests|restricted", re.I)



_TRANSIENT_RE = re.compile(r"taskgroup|targetclosed|target page, context or browser|connection|disconnected|closed", re.I)


def _looks_transient(exc: BaseException) -> bool:
    msgs = [str(exc)]
    for sub in getattr(exc, "exceptions", []) or []:
        msgs.append(str(sub))
    return any(_TRANSIENT_RE.search(m) for m in msgs)


class ToolError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class ForbiddenTool(ToolError):
    pass


class NotLoggedIn(ToolError):
    pass


class RateLimited(ToolError):
    pass


class ServerUnavailable(RuntimeError):
    pass




def unwrap_mcp_error(exc: BaseException) -> Exception:
    """Dig a meaningful error (NotLoggedIn / RateLimited / ToolError / ServerUnavailable) out of the
    ExceptionGroup the MCP streamable-http client raises when its task group fails."""
    best: Exception | None = None
    stack = [exc]
    while stack:
        e = stack.pop()
        if isinstance(e, (NotLoggedIn, RateLimited)):
            return e
        if isinstance(e, (ToolError, ServerUnavailable)) and best is None:
            best = e
        stack.extend(getattr(e, "exceptions", []) or [])
        if getattr(e, "__cause__", None):
            stack.append(e.__cause__)
    if best is not None:
        return best
    return ServerUnavailable(f"LinkedIn sidecar error: {exc}")


class LinkedInMCP:
    """Async context manager around one MCP client session."""

    def __init__(
        self,
        target: str | StdioServerParameters,
        *,
        cache: ToolCache | None = None,
        budget: Budget | None = None,
        timeout_s: float = 180.0,
        on_event=None,
    ) -> None:
        self.target = target
        self.cache = cache
        self.budget = budget
        self.timeout_s = timeout_s
        self.on_event = on_event or (lambda level, msg, **data: None)
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self.calls = 0
        self.cache_hits = 0
        self.page_loads = 0

    async def __aenter__(self) -> LinkedInMCP:
        client = Client(self.target)
        try:
            await client.__aenter__()
        except Exception as e:  # connection refused, bad URL, process failed to start
            raise ServerUnavailable(f"cannot reach LinkedIn MCP server at {self.target!r}: {e}") from e
        self._client = client
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(exc_type, exc, tb)
            finally:
                self._client = None

    async def list_tools(self) -> dict[str, dict[str, Any]]:
        assert self._client is not None
        result = await self._client.list_tools()
        out: dict[str, dict[str, Any]] = {}
        for t in result.tools:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
            out[t.name] = schema if isinstance(schema, dict) else {}
        return out

    @staticmethod
    def clean_args(args: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in args.items() if v not in (None, "", [], {})}

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, refresh: bool = False,
                   use_cache: bool = True) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise ForbiddenTool(tool, "this tool is not allowed (LeadGen is read-only on LinkedIn)")
        args = self.clean_args(args or {})
        if self.cache is not None and use_cache and not refresh:
            cached = self.cache.get(tool, args)
            if cached is not None:
                self.cache_hits += 1
                return cached
        if self._client is None:
            raise ServerUnavailable("MCP client is not connected")
        async with self._lock:
            if self.budget is not None and tool != "close_session":
                await self.budget.acquire(tool)
            self.calls += 1
            self.page_loads += 1
            self.on_event("debug", f"linkedin call {tool} {json.dumps(args, ensure_ascii=False)}")
            result = None
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(self._client.call_tool(tool, args), timeout=self.timeout_s)
                    break
                except TimeoutError as e:
                    if self.budget is not None:
                        self.budget.strike("timeout")
                    raise ToolError(tool, f"timed out after {self.timeout_s:.0f}s (is the LinkedIn session logged in?)") from e
                except (ToolError, ForbiddenTool):
                    raise
                except BaseException as e:  # noqa: BLE001 - MCP task-group / browser crashes surface here
                    if _looks_transient(e) and attempt == 0:
                        self.on_event("warning", f"{tool}: transient browser/connection error, retrying once...")
                        await asyncio.sleep(4)
                        continue
                    raise ServerUnavailable(
                        f"LinkedIn sidecar failed on {tool}: {e}. The browser may have crashed; "
                        f"stop and start it again from Settings (or restart the sidecar)."
                    ) from e
        payload = self._payload(result)
        if getattr(result, "is_error", False):
            message = self._text(result) or json.dumps(payload)[:500]
            self._raise_for(tool, message)
        self._check_section_errors(tool, payload)
        if self.budget is not None:
            self.budget.clear_strikes()
        if self.cache is not None and use_cache and tool != "close_session":
            self.cache.put(tool, args, payload)
        return payload

    # -- helpers -------------------------------------------------------------------
    @staticmethod
    def _text(result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _payload(self, result: Any) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and structured:
            return structured
        text = self._text(result)
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        return {"text": text}

    def _raise_for(self, tool: str, message: str) -> None:
        if _CHALLENGE_RE.search(message) or RATE_LIMITED_TEXT in message:
            if self.budget is not None:
                self.budget.strike(message)
            raise RateLimited(tool, message)
        if _LOGIN_RE.search(message):
            raise NotLoggedIn(tool, message)
        raise ToolError(tool, message)

    def _check_section_errors(self, tool: str, payload: dict[str, Any]) -> None:
        errors = payload.get("section_errors") or {}
        sections = payload.get("sections") or {}
        if not errors:
            return
        rate_limited = [k for k, v in errors.items() if "rate" in json.dumps(v).lower()]
        if rate_limited and not sections:
            if self.budget is not None:
                self.budget.strike("rate limited section")
            raise RateLimited(tool, f"LinkedIn rate-limited sections {rate_limited}")
        self.on_event("warning", f"{tool}: section errors {list(errors)}")


def stdio_target(command: str = "mcp-server-linkedin", extra_args: list[str] | None = None,
                 env: dict[str, str] | None = None) -> StdioServerParameters:
    import os

    base_env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "LANG")}
    base_env.update(env or {})
    return StdioServerParameters(command=command, args=extra_args or [], env=base_env)
