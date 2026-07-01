from __future__ import annotations

import pytest

from surogates.orchestrator import worker


def test_build_agent_principal_resolver_returns_callable_with_cache():
    resolver = worker.build_agent_principal_resolver(session_factory=object())

    assert callable(resolver)
    assert hasattr(resolver, "cache")


@pytest.mark.asyncio
async def test_start_worker_invalidator_passes_agent_principal_cache(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_invalidator(redis_client, **kwargs):
        captured["redis_client"] = redis_client
        captured.update(kwargs)

    monkeypatch.setattr("surogates.runtime.run_invalidator", fake_run_invalidator)

    state = {
        "redis": object(),
        "runtime_config_cache": object(),
        "file_bundle_cache": object(),
        "memory_cache": object(),
        "system_bundle_cache": object(),
        "mate_settings_cache": object(),
        "agent_principal_cache": object(),
    }

    worker._start_worker_invalidator(state)
    await state["runtime_invalidator_task"]

    assert captured["runtime_config_cache"] is state["runtime_config_cache"]
    assert captured["agent_principal_cache"] is state["agent_principal_cache"]
