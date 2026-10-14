"""Manage the vendored LinkedIn MCP sidecar process, the LinkedIn session, and doctor checks."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from leadgen.config import Settings
from leadgen.sources.linkedin_mcp import ALLOWED_TOOLS, LinkedInMCP, ServerUnavailable, ToolError
from leadgen.store.cache import ToolCache
from leadgen.store.db import Database


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class ServerManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.proc: asyncio.subprocess.Process | None = None
        self.log_lines: list[str] = []
        self._reader: asyncio.Task | None = None

    # ------------------------------------------------------------------ binaries
    def server_binary(self) -> str | None:
        candidates = [self.settings.mcp_bin, str(Path(sys.executable).with_name("mcp-server-linkedin"))]
        for c in candidates:
            found = shutil.which(c) or (c if Path(c).exists() else None)
            if found:
                return found
        return None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # --------------------------------------------------------------- lifecycle
    async def start(self, extra_args: list[str] | None = None) -> str:
        if self.running():
            return "already running"
        binary = self.server_binary()
        if not binary:
            raise RuntimeError("mcp-server-linkedin not found; run `uv sync` in the project")
        args = [binary, "--transport", "streamable-http", "--host", self.settings.mcp_host, "--port",
                str(self.settings.mcp_port), "--path", self.settings.mcp_path, "--log-level", "INFO",
                "--tool-timeout", str(int(self.settings.mcp_tool_timeout_s))]
        args += extra_args or []
        self.log_lines.clear()
        self.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT, env=_child_env())
        self._reader = asyncio.create_task(self._pump())
        for _ in range(60):
            await asyncio.sleep(0.5)
            if not self.running():
                return "exited: " + "\n".join(self.log_lines[-10:])
            if await self.healthy():
                return "started"
        return "started (not healthy yet)"

    async def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            self.log_lines.append(line.decode(errors="replace").rstrip())
            del self.log_lines[:-400]

    async def stop(self) -> str:
        if not self.running():
            self.proc = None
            return "not running"
        assert self.proc
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=15)
        except TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None
        return "stopped"

    async def healthy(self) -> bool:
        try:
            async with LinkedInMCP(self.settings.mcp_url, timeout_s=10) as mcp:
                tools = await mcp.list_tools()
                return "search_people" in tools
        except Exception:
            return False

    async def tool_schemas(self) -> dict[str, dict[str, Any]]:
        async with LinkedInMCP(self.settings.mcp_url, timeout_s=10) as mcp:
            return await mcp.list_tools()

    # ------------------------------------------------------------------ session
    async def _one_shot(self, args: list[str], timeout_s: float) -> tuple[int, str]:
        binary = self.server_binary()
        if not binary:
            return 1, "mcp-server-linkedin not found; run `uv sync`"
        was_running = self.running()
        if was_running:
            await self.stop()
        proc = await asyncio.create_subprocess_exec(binary, *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env=_child_env())
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            text = out.decode(errors="replace")
        except TimeoutError:
            proc.kill()
            text = f"timed out after {timeout_s:.0f}s"
        if was_running:
            await self.start()
        return (proc.returncode or 0), text[-4000:]

    async def manual_login(self) -> tuple[int, str]:
        """Opens a visible Chromium window; the user signs in; the session is saved for the sidecar."""
        return await self._one_shot(["--login", "--no-headless", "--login-timeout", "900"], timeout_s=960)

    async def import_from_browser(self, browser: str | None = "chrome") -> tuple[int, str]:
        args = ["--import-from-browser"] + ([browser] if browser else [])
        return await self._one_shot(args, timeout_s=120)

    async def logout(self) -> tuple[int, str]:
        return await self._one_shot(["--logout"], timeout_s=60)

    async def session_check(self) -> dict[str, Any]:
        """Cheap probe: get_my_profile through the cache (costs one page load the first time per day)."""
        try:
            async with LinkedInMCP(self.settings.mcp_url, cache=ToolCache(self.db), timeout_s=120) as mcp:
                payload = await mcp.call("get_my_profile", {})
                url = payload.get("url", "")
                return {"ok": bool(url), "url": url}
        except ServerUnavailable as e:
            return {"ok": False, "error": f"server unavailable: {e}"}
        except ToolError as e:
            return {"ok": False, "error": e.message}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------- doctor
    async def doctor(self, live: bool = True) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        binary = self.server_binary()
        checks.append({"check": "mcp-server-linkedin binary", "ok": bool(binary), "detail": binary or "run `uv sync`"})
        chromium = _chromium_present()
        checks.append({"check": "Chromium for Patchright", "ok": chromium is not None,
                       "detail": chromium or "run `uv run patchright install chromium` (the sidecar also installs it on first use)"})
        try:
            self.db.execute("SELECT 1")
            checks.append({"check": "database", "ok": True, "detail": str(self.settings.database)})
        except Exception as e:  # noqa: BLE001
            checks.append({"check": "database", "ok": False, "detail": str(e)})
        claude = shutil.which(self.settings.claude_bin)
        checks.append({"check": "claude CLI", "ok": bool(claude), "detail": claude or "install Claude Code for LLM assist (optional)"})
        if live:
            healthy = await self.healthy()
            detail = self.settings.mcp_url
            if healthy:
                schemas = await self.tool_schemas()
                has_page = "page" in (schemas.get("search_people", {}).get("properties") or {})
                missing = sorted(ALLOWED_TOOLS - set(schemas) - {"close_session"})
                detail += f" | page param: {'yes' if has_page else 'NO (unpatched server?)'}"
                if missing:
                    detail += f" | missing tools: {missing}"
            else:
                detail += " not reachable (start it from Settings)"
            checks.append({"check": "LinkedIn sidecar", "ok": healthy, "detail": detail})
            if healthy:
                sess = await self.session_check()
                checks.append({"check": "LinkedIn session", "ok": sess.get("ok", False),
                               "detail": sess.get("url") or sess.get("error", "")})
            if claude:
                from leadgen.llm.claude_cli import ClaudeCLI

                ping = await ClaudeCLI(binary=self.settings.claude_bin, model=self.settings.claude_model,
                                       effort=self.settings.claude_effort, cwd=self.settings.claude_cwd, timeout_s=120).ping()
                checks.append({"check": "claude login", "ok": ping.get("ok", False),
                               "detail": ping.get("error") or f"model {ping.get('model')}"})
        return checks


def _chromium_present() -> str | None:
    roots = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright",
             Path.home() / "AppData/Local/ms-playwright"]
    for root in roots:
        if root.exists():
            hits = sorted(p for p in root.iterdir() if p.name.startswith("chromium") and p.is_dir())
            if hits:
                return str(hits[-1]) + ("" if len(hits) == 1 else f" (+{len(hits) - 1} more)")
    return None
