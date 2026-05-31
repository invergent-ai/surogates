"""Tests for platform_cleanup + platform_idle_reset.

Both scripts share the iterate-agents + run-per-agent shape; 
tests share fakes.
"""

from __future__ import annotations

import pytest


async def _yield_agents(agents):
    for a in agents:
        yield a


@pytest.mark.asyncio
async def test_platform_cleanup_iterates_and_collects_outcomes():
    from surogates.jobs.platform_cleanup import run_platform_cleanup

    seen: list[str] = []

    async def cleanup_for_agent(agent):
        seen.append(agent["id"])
        return {"freed_keys": 3}

    outcomes = await run_platform_cleanup(
        agent_iter=lambda: _yield_agents([
            {"id": "a-1"}, {"id": "a-2"}, {"id": "a-3"},
        ]),
        cleanup_for_agent=cleanup_for_agent,
    )

    assert seen == ["a-1", "a-2", "a-3"]
    assert outcomes == {
        "a-1": {"freed_keys": 3},
        "a-2": {"freed_keys": 3},
        "a-3": {"freed_keys": 3},
    }


@pytest.mark.asyncio
async def test_platform_cleanup_per_agent_failure_isolated():
    """A per-agent failure is captured in the outcomes dict and
    iteration continues -- one tenant's broken storage backend
    must NOT stop cleanup for other tenants."""
    from surogates.jobs.platform_cleanup import run_platform_cleanup

    async def cleanup_for_agent(agent):
        if agent["id"] == "a-2":
            raise RuntimeError("storage down")
        return {"freed_keys": 1}

    outcomes = await run_platform_cleanup(
        agent_iter=lambda: _yield_agents([
            {"id": "a-1"}, {"id": "a-2"}, {"id": "a-3"},
        ]),
        cleanup_for_agent=cleanup_for_agent,
    )

    assert outcomes["a-1"] == {"freed_keys": 1}
    assert "error" in outcomes["a-2"]
    assert "RuntimeError" in outcomes["a-2"]["error"]
    assert outcomes["a-3"] == {"freed_keys": 1}


@pytest.mark.asyncio
async def test_platform_cleanup_skips_agents_with_no_id():
    """Defensive: an agent dict missing 'id' (e.g. a malformed
    api response) is logged and skipped, not crashed on."""
    from surogates.jobs.platform_cleanup import run_platform_cleanup

    async def cleanup_for_agent(agent):
        return {"ok": True}

    outcomes = await run_platform_cleanup(
        agent_iter=lambda: _yield_agents([
            {"id": "a-1"}, {"missing": "id"},
        ]),
        cleanup_for_agent=cleanup_for_agent,
    )

    assert "a-1" in outcomes
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_platform_idle_reset_iterates_and_collects_outcomes():
    from surogates.jobs.platform_idle_reset import (
        run_platform_idle_reset,
    )

    seen: list[str] = []

    async def idle_reset(agent):
        seen.append(agent["id"])
        return {"reset_count": 2}

    outcomes = await run_platform_idle_reset(
        agent_iter=lambda: _yield_agents([
            {"id": "a-1"}, {"id": "a-2"},
        ]),
        idle_reset_for_agent=idle_reset,
    )

    assert seen == ["a-1", "a-2"]
    assert outcomes == {
        "a-1": {"reset_count": 2},
        "a-2": {"reset_count": 2},
    }


@pytest.mark.asyncio
async def test_platform_idle_reset_per_agent_failure_isolated():
    from surogates.jobs.platform_idle_reset import (
        run_platform_idle_reset,
    )

    async def idle_reset(agent):
        if agent["id"] == "a-2":
            raise RuntimeError("sandbox down")
        return {"reset_count": 1}

    outcomes = await run_platform_idle_reset(
        agent_iter=lambda: _yield_agents([
            {"id": "a-1"}, {"id": "a-2"}, {"id": "a-3"},
        ]),
        idle_reset_for_agent=idle_reset,
    )

    assert outcomes["a-1"] == {"reset_count": 1}
    assert "error" in outcomes["a-2"]
    assert outcomes["a-3"] == {"reset_count": 1}


@pytest.mark.asyncio
async def test_platform_cleanup_main_uses_defaults_when_no_args():
    """main() with no kwargs uses the NotImplementedError-raising
    defaults; we don't exercise them (the Plan 8 follow-up
    wires the real defaults), but we assert main() is callable
    and routes through run_platform_cleanup."""
    from surogates.jobs import platform_cleanup as mod

    captured = {}

    async def fake_run(*, agent_iter, cleanup_for_agent):
        captured["agent_iter"] = agent_iter
        captured["cleanup_for_agent"] = cleanup_for_agent
        return {}

    original = mod.run_platform_cleanup
    mod.run_platform_cleanup = fake_run
    try:
        result = await mod.main()
    finally:
        mod.run_platform_cleanup = original

    assert result == {}
    assert captured["agent_iter"] is mod._default_agent_iter
    assert captured["cleanup_for_agent"] is (
        mod._default_cleanup_for_agent
    )
