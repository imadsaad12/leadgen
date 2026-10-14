import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from leadgen.config import Settings
from leadgen.pipeline.run import Pipeline
from leadgen.sources.fake import FakeSource
from leadgen.store.db import Database
from leadgen.store.events import EventBus
from leadgen.store.runs import RunStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "home", db_path=tmp_path / "home" / "test.db", out_dir=tmp_path / "out")
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.database)
    yield d
    d.close()


@pytest.fixture
def store(db: Database) -> RunStore:
    return RunStore(db)


@pytest.fixture
def events(db: Database) -> EventBus:
    return EventBus(db)


@pytest.fixture
def pipeline(settings: Settings, db: Database, store: RunStore, events: EventBus) -> Pipeline:
    return Pipeline(settings, db, store, events)


@pytest.fixture
def fake_factory():
    source = FakeSource()

    @asynccontextmanager
    async def factory(ctx):
        yield source

    factory.source = source  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def run_pipeline(pipeline, fake_factory):
    async def _run(run, llm=None, pause=None, stop=None):
        return await pipeline.execute(run, fake_factory, pause=pause or asyncio.Event(), stop=stop or asyncio.Event(), llm=llm)

    return _run
