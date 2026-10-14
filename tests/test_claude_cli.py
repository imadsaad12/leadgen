import asyncio
import json

import pytest

from leadgen.llm.claude_cli import ClaudeCLI, ClaudeCLIError, ClaudeCLIUnavailable
from leadgen.llm.schemas import TitleBatch


class FakeProc:
    def __init__(self, out: bytes, code: int = 0):
        self.out, self.returncode = out, code

    async def communicate(self, inp=None):
        return self.out, b""

    def kill(self):
        pass


@pytest.mark.asyncio
async def test_structured_parses_envelope_and_caches(monkeypatch):
    calls = []
    envelope = {"is_error": False, "subtype": "success", "num_turns": 2, "total_cost_usd": 0.01, "usage": {"input_tokens": 5},
                "structured_output": {"items": [{"raw_title": "Wizard", "department": "other", "seniority": "mid",
                                                  "canonical_title": "Wizard"}]}}

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc(json.dumps(envelope).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ClaudeCLI, "resolve_binary", lambda self: "/bin/claude")
    store = {}
    cli = ClaudeCLI(cache_get=store.get, cache_put=lambda k, kind, resp, usage: store.__setitem__(k, resp))
    result = await cli.structured("classify", TitleBatch, system="sys", kind="titles")
    assert result.items[0].department.value == "other"
    args = calls[0]
    assert "--json-schema" in args and "--tools" in args and "--max-turns" in args and args[args.index("--max-turns") + 1] == "3"
    assert "--bare" not in args
    again = await cli.structured("classify", TitleBatch, system="sys", kind="titles")
    assert again.items[0].raw_title == "Wizard" and len(calls) == 1  # served from cache


@pytest.mark.asyncio
async def test_error_envelope_and_missing_binary(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"}).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ClaudeCLI, "resolve_binary", lambda self: "/bin/claude")
    with pytest.raises(ClaudeCLIError):
        await ClaudeCLI().structured("p", TitleBatch, system="s", use_cache=False)
    monkeypatch.setattr(ClaudeCLI, "resolve_binary", lambda self: None)
    with pytest.raises(ClaudeCLIUnavailable):
        await ClaudeCLI().structured("p", TitleBatch, system="s", use_cache=False)


def test_child_env_strips_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = ClaudeCLI.child_env()
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env
