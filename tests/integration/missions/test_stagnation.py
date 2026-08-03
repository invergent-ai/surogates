"""The evaluator remembers whether it has said this before.

`apply_verdict` writes `last_evaluation_result/explanation/feedback` as
single slots, so round N had no knowledge of round N-1: a mission could be
told "needs revision" for the same reason indefinitely, and every round the
judge re-derived that opinion from scratch at full cost.

This is deliberately NOT LoopX's progress-granularity lattice. That
classifies delivery size from free text via substring matching, and the
plan's own verification found its enforcement path does not exist in the
shape described. The signal that matters here needs no taxonomy: the
evaluator keeps returning the same non-satisfied verdict.
"""
from __future__ import annotations

import pytest

from surogates.missions.store import MissionStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mission(session_factory, org_id, user_id, chat_session, **kw):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works", **kw,
    )
    return store, mid


async def test_a_fresh_mission_is_not_stagnant(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    assert (await store.get(mid)).stagnant_evaluations == 0


async def test_repeated_non_progress_accumulates(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for expected in (1, 2, 3):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="not there yet",
            feedback="keep going",
        )
        assert (await store.get(mid)).stagnant_evaluations == expected


async def test_progress_resets_the_streak(
    session_factory, org_id, user_id, chat_session,
):
    """A satisfied verdict means the loop moved; the count starts over."""
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    await store.record_evaluation(
        mid, result="needs_revision", explanation="no", feedback="f",
    )
    await store.record_evaluation(
        mid, result="satisfied", explanation="done", feedback="",
    )
    assert (await store.get(mid)).stagnant_evaluations == 0


async def test_the_streak_reaches_the_terminal(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(3):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="no", feedback="f",
        )
    assert await store.is_stagnant(mid) is True


async def test_below_the_terminal_is_not_stagnant(
    session_factory, org_id, user_id, chat_session,
):
    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(2):
        await store.record_evaluation(
            mid, result="needs_revision", explanation="no", feedback="f",
        )
    assert await store.is_stagnant(mid) is False


async def test_the_judge_is_told_it_has_been_here_before(
    session_factory, org_id, user_id, chat_session,
):
    """The load-bearing change: the evaluator already ran every round, it
    simply had no memory of having done so."""
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    for _ in range(2):
        await store.record_evaluation(
            mid, result="needs_revision",
            explanation="the tests still fail", feedback="fix the tests",
        )

    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="tried again",
        session_factory=session_factory, mission_store=store,
    )
    assert "2" in prompt
    assert "the tests still fail" in prompt


async def test_a_first_pass_prompt_carries_no_history(
    session_factory, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import build_evaluator_prompt

    store, mid = await _mission(session_factory, org_id, user_id, chat_session)
    prompt = await build_evaluator_prompt(
        mission_id=mid, coordinator_last_response="first try",
        session_factory=session_factory, mission_store=store,
    )
    assert "previous evaluation" not in prompt.lower()
