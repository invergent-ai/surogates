"""Integration test: the mission evaluator hook skips a research mission
while an experiment is in flight (no verdict, no iteration burn)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from surogates.arbor.store import ResearchStore
from surogates.harness.loop_mission_evaluator import _maybe_run_mission_evaluator
from surogates.missions.store import MissionStore


class _StubStore:
    def __init__(self):
        self.events: list[str] = []

    async def emit_event(self, session_id, event_type, payload):
        self.events.append(getattr(event_type, "value", str(event_type)))
        return None


@pytest.mark.asyncio(loop_scope="session")
async def test_evaluator_skips_while_experiment_in_flight(
    session_factory, seeded_org_and_session,
):
    org_id, mission_id, session_id = seeded_org_and_session  # seeded mission is active
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/wire/trunk", branch_prefix="research/wire",
        objective="o",
    )
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    await store.update_node(run_id, "1", status="running")  # in flight

    judge = AsyncMock(side_effect=AssertionError("judge must not run while in flight"))
    await _maybe_run_mission_evaluator(
        session_id=session_id,
        coordinator_last_response="I dispatched experiments.",
        session_store=_StubStore(),
        session_factory=session_factory,
        mission_store=MissionStore(session_factory),
        judge=judge,
    )
    judge.assert_not_called()
    # (The standard, non-research mission path — judge IS consulted on a
    # real trigger — is covered by the existing tests/missions suite, which
    # would break if the research early-return shadowed standard missions.)
