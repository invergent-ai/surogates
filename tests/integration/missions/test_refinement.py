"""Evidence-gated rubric refinement, end to end over a real database.

A mission whose rubric is the defect used to have three exits, all losses:
terminate `blocked`, terminate `failed`, or grind out max_iterations against
a target it cannot hit. Here the judge authors a replacement, the user
authorizes it, and the harness applies the *recorded* text.

See docs/superpowers/specs/2026-08-11-mission-objective-refinement-design.md.
"""
from __future__ import annotations

import pytest

from surogates.missions.store import MissionStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mission(session_factory, org_id, user_id, chat_session, **kw):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator", description="ship the bundle",
        rubric="Satisfied when dist/app.js exists.", **kw,
    )
    return store, mid


async def test_a_stagnant_mission_is_told_the_rubric_may_be_the_defect(
    session_factory, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(3):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="still no dist/app.js",
            feedback="run the build",
        )

    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="built again",
        session_factory=session_factory, mission_store=store,
    )
    assert "proposed_rubric" in prompt


async def test_a_merely_slow_mission_is_not_nudged_to_repropose(
    session_factory, org_id, user_id, chat_session,
):
    """Below the stagnation limit the hint stays out -- a rubric the work has
    not yet satisfied is needs_revision, not a proposal."""
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(2):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="still building",
            feedback="keep going",
        )

    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="building",
        session_factory=session_factory, mission_store=store,
    )
    assert "proposed_rubric" not in prompt
