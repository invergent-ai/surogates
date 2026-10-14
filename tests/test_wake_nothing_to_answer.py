"""A conversation revived for a message that never arrived runs no turn.

The inbound pipeline revives an idle channel conversation (``session.resume``
with source ``channel_message``) and shows the AI disclosure before its plan
gate can drop the message.  Woken on that alone, the model had no words to
answer and wrote from its instructions, which the channel sent the person.
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


REVIVED = [
    _event(11, EventType.SESSION_RESUME, source="channel_message"),
    _event(12, EventType.DISCLOSURE_PRESENTED, level="first"),
]


def test_a_revival_and_a_disclosure_are_nothing_to_answer():
    assert _nothing_to_answer(REVIVED)
    assert not _nothing_to_answer([])
    assert not _nothing_to_answer([*REVIVED, _event(13, EventType.USER_MESSAGE, content="hi")])
    # A retry the user asked for is work.
    assert not _nothing_to_answer([_event(11, EventType.SESSION_RESUME, source="user_retry")])


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
        *REVIVED,
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
