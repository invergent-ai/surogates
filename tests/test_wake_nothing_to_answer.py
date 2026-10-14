"""A message refused for want of a plan wakes no turn.

The inbound pipeline used to revive an idle channel conversation before its
plan gate dropped the message.  Woken on that revival alone, the model had no
words to answer and wrote from its instructions, which the channel sent the
person.  The conversation is now revived only once the gate lets the message
through, and a wake that finds only held messages runs no model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.loop_pending import _nothing_to_answer
from surogates.session.events import EventType
from tests.test_wake_stranded_user_message import _harness, _patch_resolver

from surogates.session.models import Session


def _event(event_id: int, kind: EventType, **data: Any) -> Any:
    return SimpleNamespace(id=event_id, type=kind.value, data=data)


HELD = [
    _event(11, EventType.DISCLOSURE_PRESENTED, level="first"),
    _event(12, EventType.USER_MESSAGE, content="Bună", held="subscription_required"),
]


def test_a_held_message_and_a_disclosure_are_nothing_to_answer():
    assert _nothing_to_answer(HELD)
    assert not _nothing_to_answer([])
    assert not _nothing_to_answer([*HELD, _event(13, EventType.USER_MESSAGE, content="hi")])
    # A revival is work: only a message the gate let through revives a conversation.
    assert not _nothing_to_answer([_event(11, EventType.SESSION_RESUME, source="channel_message")])


@pytest.mark.asyncio
async def test_a_wake_with_nothing_to_answer_runs_no_model(monkeypatch):
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1",
        channel="telegram", status="active", config={}, created_at=now, updated_at=now,
    )
    history = [
        _event(5, EventType.USER_MESSAGE, content="Am cumpărat pastilele"),
        _event(6, EventType.LLM_RESPONSE, message={"role": "assistant", "content": "Am notat."}),
        *HELD,
    ]
    store = AsyncMock()
    store.get_session.return_value = session
    store.get_harness_cursor = AsyncMock(return_value=6)
    store.get_events = AsyncMock(return_value=history)
    store.try_acquire_lease = AsyncMock(return_value=SimpleNamespace(lease_token=uuid4()))
    store.update_session_status = AsyncMock(return_value=None)
    harness = _harness(store)
    _patch_resolver(monkeypatch)

    await harness.wake(session.id)

    harness._llm.chat.completions.create.assert_not_called()
    store.update_session_status.assert_awaited_once_with(session.id, "completed")
    assert not [
        c for c in store.emit_event.call_args_list
        if c.args and c.args[1] in (EventType.HARNESS_WAKE, EventType.LLM_REQUEST)
    ]


def test_a_message_refused_for_want_of_a_plan_is_nothing_to_answer_alone():
    held = HELD[1]
    assert _nothing_to_answer([held])
    # Let through later, the next message brings the held one along.
    assert not _nothing_to_answer([held, _event(14, EventType.USER_MESSAGE, content="Am plătit")])


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False])
async def test_an_idle_conversation_is_revived_only_for_a_message_let_through(refused):
    from surogates.channels.inbound import ChannelInboundPipeline, InboundOutcome
    from surogates.runtime.platform_client import AllowanceExhaustedError
    from tests.test_channel_pipeline import (
        _FakeAllowanceClient,
        _capped_config,
        _make_config,
        _make_deps,
        _make_msg,
        _make_routing,
    )

    deps = _make_deps()
    deps.platform_client = _FakeAllowanceClient(
        error=AllowanceExhaustedError("allowance_exhausted") if refused else None,
        receipt=None if refused else {},
    )
    deps.runtime_config = _capped_config()
    revived: list = []

    async def _revive(store, session_id):
        revived.append(session_id)

    deps.revive_session = _revive

    result = await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, ts=f"revive.{refused}"), routing=_make_routing(),
        config=_make_config(), deps=deps,
    )

    if refused:
        assert result == InboundOutcome.DROPPED
        assert revived == [] and deps._enqueued == []
    else:
        assert result == InboundOutcome.PROCESSED
        assert len(revived) == 1 and deps._enqueued
