"""Unit tests for ``_create_session_for_task`` — the spawn primitive that
wraps the existing ``create_child_session`` so the spawn_task tool and the
dispatcher tick can share the exact same child-session-creation logic.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from surogates.session.events import EventType

from tests.tasks.conftest import _make_session, _make_store, _make_task


@pytest.mark.asyncio
async def test_create_session_for_task_passes_task_id_through_provisioning():
    """The factored spawn primitive calls create_child_session with task_id set."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="research vLLM", context="for inference deployment")
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.create_child_session",
        new=AsyncMock(return_value=child),
    ) as ccs:
        result = await _create_session_for_task(
            task,
            session_store=store,
            session_factory=None,
            tenant=MagicMock(org_id=task.org_id),
        )

    assert result.id == child.id

    # create_child_session was called with task_id, channel="task"
    ccs.assert_called_once()
    kwargs = ccs.call_args.kwargs
    assert kwargs["task_id"] == task.id
    assert kwargs["channel"] == "task"
    assert kwargs["parent"] is parent


@pytest.mark.asyncio
async def test_create_session_for_task_emits_user_message_with_goal_and_context():
    """USER_MESSAGE event on the child carries goal + context block."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="research vLLM", context="for inference deployment")
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.create_child_session",
        new=AsyncMock(return_value=child),
    ):
        await _create_session_for_task(
            task,
            session_store=store,
            session_factory=None,
            tenant=MagicMock(org_id=task.org_id),
        )

    emit_calls = store.emit_event.call_args_list
    user_msg_calls = [c for c in emit_calls if c[0][1] == EventType.USER_MESSAGE]
    assert len(user_msg_calls) == 1
    target_session_id = user_msg_calls[0][0][0]
    payload = user_msg_calls[0][0][2]
    assert target_session_id == child.id
    assert "research vLLM" in payload["content"]
    assert "for inference deployment" in payload["content"]


@pytest.mark.asyncio
async def test_create_session_for_task_emits_worker_spawned_with_task_id():
    """WORKER_SPAWNED on parent carries both worker_id and task_id."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="g", context=None)
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.create_child_session",
        new=AsyncMock(return_value=child),
    ):
        await _create_session_for_task(
            task,
            session_store=store,
            session_factory=None,
            tenant=MagicMock(org_id=task.org_id),
        )

    emit_calls = store.emit_event.call_args_list
    spawned = [c for c in emit_calls if c[0][1] == EventType.WORKER_SPAWNED]
    assert len(spawned) == 1
    target_session_id = spawned[0][0][0]
    payload = spawned[0][0][2]
    assert target_session_id == task.parent_session_id
    assert payload["worker_id"] == str(child.id)
    assert payload["task_id"] == str(task.id)
    assert payload["goal"] == "g"


@pytest.mark.asyncio
async def test_create_session_for_task_user_message_omits_context_section_when_none():
    """With no context, USER_MESSAGE is just the goal — no empty '## Context'."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="just the goal", context=None)
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.create_child_session",
        new=AsyncMock(return_value=child),
    ):
        await _create_session_for_task(
            task,
            session_store=store,
            session_factory=None,
            tenant=MagicMock(org_id=task.org_id),
        )

    emit_calls = store.emit_event.call_args_list
    user_msg = [c for c in emit_calls if c[0][1] == EventType.USER_MESSAGE][0]
    content = user_msg[0][2]["content"]
    assert content == "just the goal"
    assert "## Context" not in content


@pytest.mark.asyncio
async def test_create_session_for_task_resolves_agent_def_when_set():
    """If task.agent_def_name is set, the resolver is called and presets apply."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="g", agent_def_name="reviewer")
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    fake_agent_def = MagicMock(
        name="reviewer", max_iterations=20, policy_profile=None,
        tools=["read_file"], disallowed_tools=None, model="claude-sonnet-4",
    )

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.create_child_session",
        new=AsyncMock(return_value=child),
    ) as ccs, patch(
        "surogates.tasks.spawn.resolve_agent_by_name",
        new=AsyncMock(return_value=fake_agent_def),
    ) as resolver:
        await _create_session_for_task(
            task,
            session_store=store,
            session_factory=None,
            tenant=MagicMock(org_id=task.org_id),
        )

    resolver.assert_called_once()
    # Child config should carry the agent_type marker and the model.
    kwargs = ccs.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4"
    cfg = kwargs["config"]
    assert cfg["agent_type"] == "reviewer"
    # max_iterations is clipped by the agent_def cap.
    assert cfg["max_iterations"] <= 20


@pytest.mark.asyncio
async def test_create_session_for_task_unknown_agent_def_raises():
    """If agent_def_name resolves to None, raise — silent fallback would mask config bugs."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="g", agent_def_name="nonexistent")
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)

    with patch(
        "surogates.tasks.spawn.resolve_agent_by_name",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(ValueError, match="nonexistent"):
            await _create_session_for_task(
                task,
                session_store=store,
                session_factory=None,
                tenant=MagicMock(org_id=task.org_id),
            )
