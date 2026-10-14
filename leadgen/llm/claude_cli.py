"""LLM assist through the local `claude` CLI (Claude subscription login). No API key is ever used.

Verified invocation (Claude Code 2.1.x): `claude -p --output-format json --json-schema <schema> --tools ""
--strict-mcp-config --mcp-config '{"mcpServers":{}}' --max-turns 3 --no-session-persistence --system-prompt ...`.
`--json-schema` delivers the result through an internal tool turn, so `--max-turns` must be >= 2. `--bare` must not
be used: it never reads OAuth credentials. CLAUDECODE and ANTHROPIC_API_KEY are removed from the child environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from leadgen.llm.schemas import PingResult

T = TypeVar("T", bound=BaseModel)


class ClaudeCLIError(RuntimeError):
    pass


class ClaudeCLIUnavailable(ClaudeCLIError):
    pass


class ClaudeCLI:
    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "sonnet",
        effort: str = "low",
        cwd: Path | None = None,
        timeout_s: float = 180.0,
        cache_get=None,
        cache_put=None,
        on_usage=None,
    ) -> None:
        self.binary = binary
        self.model = model
        self.effort = effort
        self.cwd = cwd
        self.timeout_s = timeout_s
        self._cache_get = cache_get
        self._cache_put = cache_put
        self._on_usage = on_usage or (lambda usage: None)
        self._lock = asyncio.Lock()
        self.calls = 0

    # ---------------------------------------------------------------- availability
    def resolve_binary(self) -> str | None:
        return shutil.which(self.binary)

    def available(self) -> bool:
        return self.resolve_binary() is not None

    @staticmethod
    def child_env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env.setdefault("TERM", "dumb")
        return env

    async def ping(self) -> dict[str, Any]:
        """Tiny live call to prove the CLI is installed and logged in."""
        binary = self.resolve_binary()
        if not binary:
            return {"ok": False, "error": f"`{self.binary}` not found on PATH"}
        try:
            result = await self.structured("Reply with ok=true.", PingResult, system="Return structured output only.",
                                           use_cache=False)
            return {"ok": bool(result.ok), "model": self.model}
        except ClaudeCLIError as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------- calling
    def _args(self, schema: dict[str, Any], system: str) -> list[str]:
        return [
            self.resolve_binary() or self.binary,
            "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--tools", "",
            "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--max-turns", "3",
            "--no-session-persistence",
            "--system-prompt", system,
            "--model", self.model,
            "--effort", self.effort,
        ]

    @staticmethod
    def cache_key(kind: str, system: str, prompt: str, schema: dict[str, Any], model: str) -> str:
        h = hashlib.sha256()
        for part in (kind, model, system, prompt, json.dumps(schema, sort_keys=True)):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    async def structured(self, prompt: str, schema_model: type[T], *, system: str, kind: str = "generic",
                         use_cache: bool = True) -> T:
        schema = schema_model.model_json_schema()
        key = self.cache_key(kind, system, prompt, schema, self.model)
        if use_cache and self._cache_get is not None:
            cached = self._cache_get(key)
            if cached is not None:
                try:
                    return schema_model.model_validate(cached)
                except ValidationError:
                    pass
        if not self.available():
            raise ClaudeCLIUnavailable(f"`{self.binary}` not found on PATH; install Claude Code and run `claude login`")
        last_error: str | None = None
        for _attempt in range(2):
            raw, usage = await self._run(self._args(schema, system), prompt)
            try:
                parsed = schema_model.model_validate(raw)
            except ValidationError as e:
                last_error = f"invalid structured output: {e}"
                continue
            self.calls += 1
            self._on_usage(usage)
            if use_cache and self._cache_put is not None:
                self._cache_put(key, kind, parsed.model_dump(mode="json"), usage)
            return parsed
        raise ClaudeCLIError(last_error or "claude returned no usable output")

    async def _run(self, args: list[str], prompt: str) -> tuple[Any, dict[str, Any]]:
        if self.cwd is not None:
            self.cwd.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.cwd) if self.cwd else None,
                    env=self.child_env(),
                )
            except FileNotFoundError as e:
                raise ClaudeCLIUnavailable(f"cannot start `{args[0]}`: {e}") from e
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=self.timeout_s)
            except TimeoutError as e:
                proc.kill()
                raise ClaudeCLIError(f"claude timed out after {self.timeout_s:.0f}s") from e
        if proc.returncode != 0 and not out.strip():
            raise ClaudeCLIError(f"claude exited {proc.returncode}: {err.decode(errors='replace')[:400]}")
        try:
            envelope = json.loads(out.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise ClaudeCLIError(f"claude returned non-JSON output: {out[:200]!r}") from e
        if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
            msg = envelope.get("result") or envelope.get("subtype") or "unknown error"
            if "login" in str(msg).lower() or "auth" in str(msg).lower() or "api key" in str(msg).lower():
                raise ClaudeCLIUnavailable(f"claude is not logged in: {msg}")
            raise ClaudeCLIError(f"claude error: {msg}")
        usage = {
            "usage": envelope.get("usage"),
            "model_usage": envelope.get("modelUsage"),
            "cost_usd": envelope.get("total_cost_usd"),
            "duration_ms": envelope.get("duration_ms"),
            "num_turns": envelope.get("num_turns"),
        }
        structured = envelope.get("structured_output")
        if structured is None:
            text = envelope.get("result") or ""
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as e:
                raise ClaudeCLIError("claude returned no structured_output") from e
        return structured, usage


def build_claude_cli(settings, store=None, events=None, run_id: str | None = None) -> ClaudeCLI:
    """Factory wired to the run store's llm_cache and the event log."""
    cache_get = store.llm_get if store is not None else None
    cache_put = store.llm_put if store is not None else None

    def on_usage(usage: dict[str, Any]) -> None:
        if events is not None:
            events.debug(run_id, "claude call", **{k: v for k, v in usage.items() if k in ("cost_usd", "duration_ms")})

    return ClaudeCLI(
        binary=settings.claude_bin,
        model=settings.claude_model,
        effort=settings.claude_effort,
        cwd=settings.claude_cwd,
        timeout_s=settings.claude_timeout_s,
        cache_get=cache_get,
        cache_put=cache_put,
        on_usage=on_usage,
    )
