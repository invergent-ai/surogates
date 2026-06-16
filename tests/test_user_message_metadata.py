"""Tests for per-message metadata on user.message events.

Covers the wire-format extension introduced for the Platform Copilot
view-context handoff:

1. ``SendMessageRequest`` accepts and round-trips an optional ``metadata`` dict.
2. The send-message route writes ``metadata`` into the ``user.message`` event
   payload via ``store.emit_event``.
3. The harness builds a per-turn view-context system note when
   ``metadata.view_context`` is set on the latest user message.
4. No note is produced when ``view_context`` is absent or malformed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException

from surogates.api.routes.sessions import SendMessageRequest, send_message
from surogates.harness.loop import _view_context_note
from surogates.session.events import EventType
from surogates.session.models import Session


# ---------------------------------------------------------------------------
# SendMessageRequest schema
# ---------------------------------------------------------------------------


def test_send_message_request_defaults_metadata_to_none():
    req = SendMessageRequest(content="hi")
    assert req.metadata is None


def test_send_message_request_round_trips_metadata():
    payload = {
        "view_context": {
            "kind": "evaluation",
            "id": "eval_42",
            "name": "Smoke suite",
        },
        "client": "studio",
    }
    req = SendMessageRequest(content="hi", metadata=payload)
    assert req.metadata == payload
    # The model must also serialize cleanly back to a dict.
    assert req.model_dump()["metadata"] == payload


def test_send_message_request_accepts_empty_metadata_dict():
    req = SendMessageRequest(content="hi", metadata={})
    assert req.metadata == {}


# ---------------------------------------------------------------------------
# send_message route: metadata is forwarded into the event payload
# ---------------------------------------------------------------------------


def _stub_session(*, status: str = "active") -> Session:
    now = SimpleNamespace()  # unused -- Session uses default datetimes via factory
    from datetime import datetime, timezone

    return Session(
        id=uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="test-agent",
        channel="web",
        status=status,
        config={},
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )


class _StubInjectionDetector:
    def detect(self, content, *, source):
        return SimpleNamespace(is_injection=False, explanation="")


@pytest.fixture()
def patched_send_message(monkeypatch):
    """Patch route dependencies so ``send_message`` runs without DB/Redis/AGT."""
    # Skip the AGT prompt-injection detector -- it would try to download
    # weights on first import.
    monkeypatch.setattr(
        "surogates.api.routes.sessions._get_injection_detector",
        lambda: _StubInjectionDetector(),
    )
    # The route enqueues onto Redis after emitting the event.  Replace it
    # with an async no-op so the test does not need a Redis container.
    monkeypatch.setattr(
        "surogates.api.routes.sessions.enqueue_session",
        AsyncMock(),
    )

    async def _runner(
        *,
        metadata: dict | None,
        session_status: str = "active",
    ) -> dict:
        session = _stub_session(status=session_status)
        store = SimpleNamespace(
            emit_event=AsyncMock(return_value=123),
            update_session_status=AsyncMock(),
        )

        app = FastAPI()
        app.state.session_store = store
        app.state.redis = SimpleNamespace()
        app.state.settings = SimpleNamespace(agent_id=session.agent_id)
        request = SimpleNamespace(
            app=app,
            url=SimpleNamespace(path="/v1/sessions/abc/messages"),
        )

        # Bypass _get_session_for_tenant + require_user_writable_session.
        monkeypatch.setattr(
            "surogates.api.routes.sessions._get_session_for_tenant",
            AsyncMock(return_value=session),
        )
        monkeypatch.setattr(
            "surogates.api.routes.sessions.require_user_writable_session",
            lambda _s: None,
        )

        body = SendMessageRequest(content="hi", metadata=metadata)
        tenant = SimpleNamespace(
            user_id=session.user_id,
            org_id=session.org_id,
            service_account_id=None,
            session_scope_id=None,
        )
        agent_runtime = SimpleNamespace(agent_id=session.agent_id)

        response = await send_message(
            session_id=session.id,
            body=body,
            request=request,
            tenant=tenant,
            agent_runtime=agent_runtime,
        )
        return {
            "response": response,
            "emit_args": store.emit_event.await_args,
        }

    return _runner


async def test_send_message_forwards_metadata_into_event(patched_send_message):
    metadata = {
        "view_context": {
            "kind": "agent",
            "id": "agt_123",
            "name": "Triage bot",
        }
    }

    result = await patched_send_message(metadata=metadata)
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert event_data["content"] == "hi"
    assert event_data["metadata"] == metadata


async def test_send_message_omits_metadata_when_absent(patched_send_message):
    result = await patched_send_message(metadata=None)
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert "metadata" not in event_data


async def test_send_message_preserves_empty_metadata_dict(patched_send_message):
    """An explicit empty ``{}`` is still forwarded so callers can probe support."""
    result = await patched_send_message(metadata={})
    args, kwargs = result["emit_args"]
    event_data = args[2] if len(args) >= 3 else kwargs["data"]

    assert event_data["metadata"] == {}


# ---------------------------------------------------------------------------
# _view_context_note: per-turn system note from latest user.message
# ---------------------------------------------------------------------------


def _user_event(data: dict, event_id: int = 1, *, enum: bool = False):
    """Build a user.message event in either string or enum type form."""
    return SimpleNamespace(
        id=event_id,
        type=EventType.USER_MESSAGE if enum else EventType.USER_MESSAGE.value,
        data=data,
    )


def _generic_event(event_type: EventType, data: dict | None = None, event_id: int = 1):
    return SimpleNamespace(
        id=event_id,
        type=event_type.value,
        data=data or {},
    )


def test_view_context_note_renders_kind_id_and_name():
    events = [
        _user_event(
            {
                "content": "what is this?",
                "metadata": {
                    "view_context": {
                        "kind": "evaluation",
                        "id": "eval_42",
                        "name": "Smoke suite",
                    }
                },
            },
        ),
    ]

    note = _view_context_note(events)
    assert note == (
        "The user is currently viewing **evaluation** eval_42 (Smoke suite)."
    )


def test_view_context_note_drops_name_when_missing():
    events = [
        _user_event(
            {"metadata": {"view_context": {"kind": "agent", "id": "agt_1"}}}
        ),
    ]

    assert (
        _view_context_note(events) == "The user is currently viewing **agent** agt_1."
    )


def test_view_context_note_is_deterministic():
    """Same input yields the same string -- required for retry idempotency."""
    events = [
        _user_event(
            {
                "metadata": {
                    "view_context": {
                        "kind": "agent",
                        "id": "agt_1",
                        "name": "Triage",
                    }
                }
            }
        ),
    ]

    first = _view_context_note(events)
    second = _view_context_note(events)
    assert first == second


def test_view_context_note_returns_none_when_no_metadata():
    events = [_user_event({"content": "hello"})]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_view_context_missing():
    events = [_user_event({"metadata": {"client": "studio"}})]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_view_context_null():
    events = [_user_event({"metadata": {"view_context": None}})]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_view_context_not_dict():
    events = [_user_event({"metadata": {"view_context": "evaluation:eval_42"}})]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_kind_or_id_missing():
    events = [_user_event({"metadata": {"view_context": {"kind": "agent"}}})]
    assert _view_context_note(events) is None

    events = [_user_event({"metadata": {"view_context": {"id": "agt_1"}}})]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_no_user_events():
    events = [_generic_event(EventType.LLM_RESPONSE)]
    assert _view_context_note(events) is None


def test_view_context_note_returns_none_when_events_empty():
    assert _view_context_note([]) is None


def test_view_context_note_uses_latest_user_message_only():
    """Older user messages with view_context are ignored after a fresh user turn."""
    events = [
        _user_event(
            {
                "metadata": {
                    "view_context": {"kind": "agent", "id": "agt_old"}
                }
            },
            event_id=1,
        ),
        _generic_event(EventType.LLM_RESPONSE, event_id=2),
        _user_event({"content": "follow-up"}, event_id=3),
    ]

    # The latest user message carries no metadata, so the note is dropped
    # even though an earlier user message had a view_context.
    assert _view_context_note(events) is None


def test_view_context_note_handles_enum_type_event():
    events = [
        _user_event(
            {
                "metadata": {
                    "view_context": {"kind": "dataset", "id": "ds_9"}
                }
            },
            enum=True,
        ),
    ]
    assert (
        _view_context_note(events)
        == "The user is currently viewing **dataset** ds_9."
    )


def test_view_context_note_tolerates_non_dict_data():
    events = [SimpleNamespace(type=EventType.USER_MESSAGE.value, data=None, id=1)]
    assert _view_context_note(events) is None


def test_view_context_note_skips_non_user_events_when_searching():
    """Tool results, LLM responses, etc. between user message and tail are skipped."""
    events = [
        _user_event(
            {
                "metadata": {
                    "view_context": {
                        "kind": "training_run",
                        "id": "tr_77",
                        "name": "Pretrain epoch 3",
                    }
                }
            },
            event_id=1,
        ),
        _generic_event(EventType.LLM_RESPONSE, event_id=2),
        _generic_event(EventType.TOOL_RESULT, event_id=3),
    ]
    assert (
        _view_context_note(events)
        == "The user is currently viewing **training_run** tr_77 (Pretrain epoch 3)."
    )
