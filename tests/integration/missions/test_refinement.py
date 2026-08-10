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


async def test_amend_rubric_replaces_the_text_and_reactivates(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    await store.record_evaluation(
        mid, result="needs_revision", explanation="no", feedback="f",
    )
    await store.set_status(mid, "paused", paused_reason="awaiting_refinement")

    await store.amend_rubric(mid, new_rubric="Satisfied when dist/bundle.js exists.")

    m = await store.get(mid)
    assert m.rubric == "Satisfied when dist/bundle.js exists."
    assert m.status == "active"
    assert m.paused_reason is None
    assert m.stagnant_evaluations == 0
    assert m.description == "ship the bundle"


async def test_amend_rubric_leaves_the_iteration_allowance_alone(
    session_factory, org_id, user_id, chat_session,
):
    """The amendment changes the target, not the budget."""
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(4):
        await store.increment_iteration(mid)

    await store.amend_rubric(mid, new_rubric="Satisfied when the build is green.")

    assert (await store.get(mid)).iteration == 4


async def test_amend_rubric_rejects_empty_text(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    with pytest.raises(ValueError):
        await store.amend_rubric(mid, new_rubric="   ")


async def test_proposal_lookup_ignores_another_missions_proposal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """Events are keyed by session, not mission. A session outlives its
    missions, so a stale proposal from a terminated one must not be found."""
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    await session_store.emit_event(
        chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
        {"mission_id": "00000000-0000-0000-0000-0000000000ff",
         "proposed_rubric": "someone else's rubric"},
    )
    assert await session_store.latest_mission_proposal(chat_session.id, mid) is None

    await session_store.emit_event(
        chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
        {"mission_id": str(mid), "proposed_rubric": "ours"},
    )
    found = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert found is not None and found["proposed_rubric"] == "ours"


async def test_proposal_lookup_takes_the_newest(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for text in ("first", "second"):
        await session_store.emit_event(
            chat_session.id, EventType.MISSION_REFINEMENT_PROPOSED,
            {"mission_id": str(mid), "proposed_rubric": text},
        )
    found = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert found["proposed_rubric"] == "second"


async def test_amendment_count_is_per_mission(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.session.events import EventType

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    assert await session_store.count_mission_amendments(chat_session.id, mid) == 0

    await session_store.emit_event(
        chat_session.id, EventType.MISSION_AMENDED, {"mission_id": str(mid)},
    )
    await session_store.emit_event(
        chat_session.id, EventType.MISSION_AMENDED,
        {"mission_id": "00000000-0000-0000-0000-0000000000ff"},
    )
    assert await session_store.count_mission_amendments(chat_session.id, mid) == 1


async def _created(session_factory, session_store, org_id, user_id, chat_session):
    """A mission created through the real command handler, so the session
    config carries active_mission_id the way production does."""
    from surogates.missions.commands import handle_mission_create

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="ship the bundle",
        rubric="Satisfied when dist/app.js exists.",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator", session_store=session_store,
        session_factory=session_factory, mission_store=store,
    )
    return store, created.mission_id


_PROPOSAL = {
    "result": "blocked",
    "explanation": "the rubric names dist/app.js; the build emits dist/bundle.js",
    "feedback": "",
    "proposed_rubric": "Satisfied when dist/bundle.js exists and tests pass.",
    "refinement_evidence": "build task output tree contains only dist/bundle.js",
}


async def test_a_blocked_verdict_with_a_proposal_pauses_instead_of_terminating(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.db.models import Session as ORMSession
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )

    m = await store.get(mid)
    assert m.status == "paused"
    assert m.paused_reason == "awaiting_refinement"
    assert m.rubric == "Satisfied when dist/app.js exists."  # not applied yet

    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        assert "active_mission_id" in (sess.config or {})

    proposal = await session_store.latest_mission_proposal(chat_session.id, mid)
    assert proposal["held_verdict"] == "blocked"
    assert proposal["proposed_rubric"] == _PROPOSAL["proposed_rubric"]
    assert proposal["old_rubric"] == "Satisfied when dist/app.js exists."


async def test_a_blocked_verdict_without_a_proposal_still_terminates(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The un-split branch is unchanged."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={"result": "blocked", "explanation": "no access", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_a_satisfied_verdict_ignores_a_proposal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """Refining the criteria of a mission that just met them is the drift
    case the whole gate exists to prevent."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL, "result": "satisfied"},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "satisfied"


async def test_a_proposal_identical_to_the_standing_rubric_is_not_raised(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A no-op proposal would cost the user a round-trip to approve nothing."""
    from surogates.missions.evaluator import apply_verdict

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid,
        verdict={**_PROPOSAL,
                 "proposed_rubric": "Satisfied when dist/app.js exists."},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_the_third_proposal_terminates(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict
    from surogates.session.events import EventType

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    for _ in range(2):
        await session_store.emit_event(
            chat_session.id, EventType.MISSION_AMENDED, {"mission_id": str(mid)},
        )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    assert (await store.get(mid)).status == "blocked"


async def test_a_paused_mission_fires_no_evaluator(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """No judge calls burn while a proposal waits for the user."""
    from surogates.missions.evaluator import apply_verdict, should_evaluate

    store, mid = await _created(
        session_factory, session_store, org_id, user_id, chat_session,
    )
    await apply_verdict(
        mission_id=mid, verdict=dict(_PROPOSAL),
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="completion_claim",
    )
    decision = await should_evaluate(
        mission_id=mid, coordinator_last_response="[[mission-complete]]",
        session_factory=session_factory, mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is False
