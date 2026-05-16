"""Tests for MissionStore CRUD and constraints."""
from __future__ import annotations

import uuid

import pytest

from surogates.missions.store import (
    ActiveMissionConflictError,
    MissionNotFoundError,
    MissionStore,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_and_get(session_factory, org_id, user_id, chat_session):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="train a model", rubric="gsm8k >= 0.8",
    )
    m = await store.get(mid)
    assert m.status == "active"
    assert m.description == "train a model"
    assert m.iteration == 0
    assert m.max_iterations == 20


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_second_active_on_same_session(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="first", rubric="r",
    )
    with pytest.raises(ActiveMissionConflictError):
        await store.create(
            org_id=org_id, user_id=user_id, session_id=chat_session.id,
            agent_id="orchestrator",
            description="second", rubric="r",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_create_allowed_after_previous_terminal(
    session_factory, org_id, user_id, chat_session,
):
    """Once the previous mission is terminal, a new one is allowed."""
    store = MissionStore(session_factory)
    first = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="first", rubric="r",
    )
    await store.set_status(first, "satisfied")
    second = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="second", rubric="r2",
    )
    assert second != first


@pytest.mark.asyncio(loop_scope="session")
async def test_get_active_for_session_returns_active_or_paused(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    got = await store.get_active_for_session(chat_session.id)
    assert got.id == mid
    await store.set_status(mid, "paused", paused_reason="manual")
    got2 = await store.get_active_for_session(chat_session.id)
    assert got2.id == mid


@pytest.mark.asyncio(loop_scope="session")
async def test_get_active_for_session_none_after_terminal(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    await store.set_status(mid, "cancelled", cancelled_reason="user")
    assert await store.get_active_for_session(chat_session.id) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_record_evaluation_writes_fields(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    await store.record_evaluation(
        mid, result="needs_revision",
        explanation="not yet", feedback="try more data",
    )
    m = await store.get(mid)
    assert m.last_evaluation_result == "needs_revision"
    assert m.last_evaluation_explanation == "not yet"
    assert m.last_evaluation_feedback == "try more data"
    assert m.last_evaluation_at is not None
    assert m.evaluator_parse_failures == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_record_parse_failure_pauses_after_three(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    assert await store.record_parse_failure(mid) == 1
    assert await store.record_parse_failure(mid) == 2
    assert await store.record_parse_failure(mid) == 3
    m = await store.get(mid)
    assert m.status == "paused"
    assert m.paused_reason == "evaluator parse failure"


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_recently_evaluated(
    session_factory, org_id, user_id, chat_session,
):
    """recently_evaluated returns True for <window since last_evaluation_at."""
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    assert not await store.recently_evaluated(mid, window_seconds=30)
    await store.record_evaluation(
        mid, result="needs_revision", explanation="", feedback="",
    )
    assert await store.recently_evaluated(mid, window_seconds=30)


@pytest.mark.asyncio(loop_scope="session")
async def test_increment_iteration(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    new_iter = await store.increment_iteration(mid)
    assert new_iter == 1
    again = await store.increment_iteration(mid)
    assert again == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_get_raises_for_unknown_id(session_factory):
    store = MissionStore(session_factory)
    with pytest.raises(MissionNotFoundError):
        await store.get(uuid.uuid4())
