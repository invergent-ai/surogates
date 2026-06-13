"""Integration test for handle_research_mission_create (the /auto-research
create path): research_runs row + ROOT node + server-side baselines +
config swap + research kickoff."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from surogates.arbor.store import ResearchStore
from surogates.db.models import Session as ORMSession
from surogates.missions.commands import (
    AutoResearchCommand,
    handle_research_mission_create,
)
from surogates.missions.store import MissionStore

from .conftest import create_org, create_user


class _StubStore:
    """Minimal session_store: records emitted event types."""

    def __init__(self):
        self.events: list[str] = []

    async def emit_event(self, session_id, event_type, payload):
        self.events.append(getattr(event_type, "value", str(event_type)))
        return uuid.uuid4()


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_session(session_factory):
    """An org + user + session with NO mission yet (so create can run)."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=session_id, org_id=org_id, user_id=user_id,
            agent_id="agent-x", config={},
        ))
        await db.commit()
    return org_id, user_id, session_id


@pytest.mark.asyncio(loop_scope="session")
async def test_research_create_builds_run_root_baselines_and_config(
    session_factory, fresh_session,
):
    org_id, user_id, session_id = fresh_session
    store = ResearchStore(session_factory)
    session_store = _StubStore()
    cmd = AutoResearchCommand(
        action="create",
        description="Improve F1 on ./repo",
        rubric="- test_trunk_score improves on test_baseline_score",
        repo="/workspace/repo",
        max_iterations=40,
        baseline=0.41,
        baseline_test=0.50,
    )
    result = await handle_research_mission_create(
        cmd=cmd, session_id=session_id, org_id=org_id, agent_id="agent-x",
        session_store=session_store, session_factory=session_factory,
        mission_store=MissionStore(session_factory), user_id=user_id,
    )
    assert result.ok and result.mission_id is not None
    assert "idea_tree" in result.kickoff_content

    # research_runs row + ROOT node + server-side baselines.
    run = await store.get_run_for_mission(result.mission_id)
    assert run is not None
    assert run.repo_path == "/workspace/repo"
    assert run.meta["baseline_score"] == 0.41
    assert run.meta["test_baseline_score"] == 0.50  # machine key, written server-side
    root = await store.get_node(run.id, "ROOT")
    assert root.status == "pending"

    # mission.max_iterations honoured (not the hardcoded 20).
    mission = await MissionStore(session_factory).get(result.mission_id)
    assert mission.max_iterations == 40

    # Session config: research run active, coordinator+strict on, arbor
    # coordinator preloaded INSTEAD of the generic task-orchestrator.
    async with session_factory() as db:
        row = await db.get(ORMSession, session_id)
        cfg = row.config
    assert cfg["active_research_run_id"] == str(run.id)
    assert cfg["active_mission_id"] == str(result.mission_id)
    assert cfg["coordinator"] is True and cfg["strict_coordinator"] is True
    assert "arbor-coordinator" in cfg["preloaded_skills"]
    assert "subagent-task-orchestrator" not in cfg["preloaded_skills"]

    assert "research.defined" in session_store.events


@pytest.mark.asyncio(loop_scope="session")
async def test_research_create_requires_workspace_repo(session_factory, fresh_session):
    org_id, user_id, session_id = fresh_session
    cmd = AutoResearchCommand(
        action="create", description="x", rubric="r", repo=None,
    )
    result = await handle_research_mission_create(
        cmd=cmd, session_id=session_id, org_id=org_id, agent_id="agent-x",
        session_store=_StubStore(), session_factory=session_factory,
        mission_store=MissionStore(session_factory), user_id=user_id,
    )
    assert not result.ok and "repo=" in result.error


@pytest.mark.asyncio(loop_scope="session")
async def test_research_create_rejects_resume(session_factory, fresh_session):
    org_id, user_id, session_id = fresh_session
    cmd = AutoResearchCommand(
        action="create", description="x", rubric="r",
        repo="/workspace/repo", resume_run="abc",
    )
    result = await handle_research_mission_create(
        cmd=cmd, session_id=session_id, org_id=org_id, agent_id="agent-x",
        session_store=_StubStore(), session_factory=session_factory,
        mission_store=MissionStore(session_factory), user_id=user_id,
    )
    assert not result.ok and "resume" in result.error
