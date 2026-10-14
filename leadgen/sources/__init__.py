"""Source registry: build an async context manager that yields a SourceAdapter for a run."""

from __future__ import annotations

from contextlib import asynccontextmanager

from leadgen.config import Settings


def make_source_factory(name: str, settings: Settings):
    name = (name or "linkedin").lower()
    if name == "fake":
        from leadgen.sources.fake import FakeSource

        @asynccontextmanager
        async def fake_factory(ctx):
            yield FakeSource()

        return fake_factory
    if name == "local":
        from leadgen.sources.local import LocalSource

        @asynccontextmanager
        async def local_factory(ctx):
            yield LocalSource()

        return local_factory
    if name == "linkedin":
        from leadgen.sources.linkedin import LinkedInSource
        from leadgen.sources.linkedin_mcp import LinkedInMCP

        @asynccontextmanager
        async def linkedin_factory(ctx):
            def on_event(level: str, msg: str, **data) -> None:
                ctx.events.emit(ctx.run_id, level, msg, data or None)

            mcp = LinkedInMCP(settings.mcp_url, cache=ctx.cache, budget=ctx.budget,
                              timeout_s=settings.mcp_tool_timeout_s, on_event=on_event)
            async with mcp:
                source = LinkedInSource(mcp)
                try:
                    yield source
                finally:
                    await source.close()

        return linkedin_factory
    raise ValueError(f"unknown source {name!r}; use 'linkedin' or 'fake'")


__all__ = ["make_source_factory"]
