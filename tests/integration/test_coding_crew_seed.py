"""Integration: seeding the crew makes the three AgentDefs DB-resolvable."""

from __future__ import annotations

import pytest

from surogates.coding_agents.crew_seed import seed_coding_crew
from surogates.tools.loader import ResourceLoader

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")

_CREW = {"claude-coder", "codex-reviewer", "code-orchestrator"}


async def _load(session_factory, org_id):
    async with session_factory() as s:
        return await ResourceLoader._load_agents_from_db(s, org_id, None, "org_db")


async def test_seed_then_resolvable(session_factory):
    org_id = await create_org(session_factory)
    names = await seed_coding_crew(session_factory, org_id)
    assert set(names) == _CREW

    by = {d.name: d for d in await _load(session_factory, org_id)}
    assert _CREW.issubset(by)
    assert "run_coding_agent" in (by["claude-coder"].tools or [])
    assert "run_coding_agent" in (by["codex-reviewer"].tools or [])
    assert "spawn_task" in (by["code-orchestrator"].tools or [])
    assert "run_coding_agent" in (by["code-orchestrator"].disallowed_tools or [])
    assert by["code-orchestrator"].system_prompt.strip()


async def test_seed_is_idempotent(session_factory):
    org_id = await create_org(session_factory)
    await seed_coding_crew(session_factory, org_id)
    await seed_coding_crew(session_factory, org_id)  # second seed must not duplicate

    crew_rows = [d for d in await _load(session_factory, org_id) if d.name in _CREW]
    assert len(crew_rows) == 3  # one row per agent, not six
