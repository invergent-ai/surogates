"""The worker's database engine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.db import engine as engines
from surogates.orchestrator import worker


async def test_the_workers_engine_checks_each_connection_before_lending_it(monkeypatch):
    built: dict = {}

    class Built(Exception):
        pass

    def engine(url, **kwargs):
        built.update(kwargs)
        raise Built

    monkeypatch.setattr(engines, "create_async_engine", engine)
    monkeypatch.setattr(worker, "default_prompt_library", lambda: SimpleNamespace(validate=lambda: None))
    settings = SimpleNamespace(db=SimpleNamespace(url="postgresql+asyncpg://db/surogates", pool_size=5, pool_overflow=10))
    with pytest.raises(Built):
        await worker.run_worker(settings)
    # A project's lock lost with its connection leaves that connection dead in the pool: checked, it is never lent.
    assert built.get("pool_pre_ping") is True
