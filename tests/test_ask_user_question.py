"""Functional coverage for ask_user_question answer and cancellation workflows.

Exercises asynchronous replies, tool-call matching, session pause and handler errors."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.ask_user_question import (
    _ask_user_question_handler,
)


class FakeEvent:
    """Minimal event shape the ask_user_question handler touches."""

    __slots__ = ("id", "type", "data")

    def __init__(self, id_: int, type_: str, data: dict[str, Any]) -> None:
        self.id = id_
        self.type = type_
        self.data = data


class FakeSessionStore:
    """In-memory session store sufficient for the ask_user_question handler.

    Implements ``emit_event``, ``get_events``, ``get_session``, and
    ``renew_lease`` -- every method the ask_user_question handler reaches for.
    """

    def __init__(self, status: str = "active") -> None:
        self._events: list[FakeEvent] = []
        self._next_id = 0
        self.renewed: int = 0
        self.status = status

    async def emit_event(
        self, session_id: Any, type_: EventType | str, data: dict[str, Any],
    ) -> int:
        self._next_id += 1
        type_str = type_.value if isinstance(type_, EventType) else type_
        self._events.append(FakeEvent(self._next_id, type_str, data))
        return self._next_id

    async def get_events(
        self,
        session_id: Any,
        *,
        after: int | None = None,
        limit: int | None = None,
        types: list[EventType] | None = None,
    ) -> list[FakeEvent]:
        after = after or 0
        type_strs = {t.value for t in types} if types else None
        out = []
        for ev in self._events:
            if ev.id <= after:
                continue
            if type_strs and ev.type not in type_strs:
                continue
            out.append(ev)
            if limit is not None and len(out) >= limit:
                break
        return out

    async def get_session(self, session_id: Any) -> Any:
        return SimpleNamespace(id=session_id, status=self.status)

    async def renew_lease(
        self, session_id: Any, lease_token: Any, ttl_seconds: int = 60,
    ) -> None:
        self.renewed += 1


@pytest.mark.asyncio
async def test_handler_returns_responses_when_matching_event_arrives():
    session_id = uuid4()
    tool_call_id = "call_xyz"
    store = FakeSessionStore()

    args = {
        "questions": [
            {"prompt": "Pick one", "choices": [{"label": "A"}, {"label": "B"}]},
        ],
    }

    async def responder() -> None:
        # Give the handler one poll cycle, then emit the response.
        await asyncio.sleep(0.05)
        await store.emit_event(
            session_id,
            EventType.ASK_USER_QUESTION_RESPONSE,
            {
                "tool_call_id": tool_call_id,
                "responses": [
                    {"question": "Pick one", "answer": "A", "is_other": False},
                ],
            },
        )

    async def invoke() -> str:
        return await _ask_user_question_handler(
            args,
            session_id=session_id,
            session_store=store,
            tool_call_id=tool_call_id,
            lease_token=uuid4(),
        )

    result_raw, _ = await asyncio.gather(invoke(), responder())
    result = json.loads(result_raw)
    assert result["cancelled"] is False
    assert result["responses"] == [
        {"question": "Pick one", "answer": "A", "is_other": False},
    ]
    # Questions carried through so the LLM sees what was asked.
    assert result["questions_asked"][0]["prompt"] == "Pick one"


@pytest.mark.asyncio
async def test_handler_returns_cancelled_when_session_is_paused():
    session_id = uuid4()
    tool_call_id = "call_abc"
    store = FakeSessionStore()

    async def pauser() -> None:
        # Flip status a beat after the handler starts polling.
        await asyncio.sleep(0.05)
        store.status = "paused"

    async def invoke() -> str:
        return await _ask_user_question_handler(
            {"questions": [{"prompt": "q"}]},
            session_id=session_id,
            session_store=store,
            tool_call_id=tool_call_id,
            lease_token=uuid4(),
        )

    result_raw, _ = await asyncio.gather(invoke(), pauser())
    result = json.loads(result_raw)
    assert result["cancelled"] is True
    assert result["reason"] == "session.paused"


@pytest.mark.asyncio
async def test_handler_ignores_response_for_other_tool_call():
    session_id = uuid4()
    my_tool_id = "mine"
    other_tool_id = "somebody_else"
    store = FakeSessionStore()

    async def chatter() -> None:
        # First, an unrelated response -- must be ignored.
        await asyncio.sleep(0.05)
        await store.emit_event(
            session_id,
            EventType.ASK_USER_QUESTION_RESPONSE,
            {
                "tool_call_id": other_tool_id,
                "responses": [
                    {"question": "q", "answer": "nope", "is_other": False},
                ],
            },
        )
        # Then the actual one we want.
        await asyncio.sleep(0.05)
        await store.emit_event(
            session_id,
            EventType.ASK_USER_QUESTION_RESPONSE,
            {
                "tool_call_id": my_tool_id,
                "responses": [
                    {"question": "q", "answer": "yes", "is_other": False},
                ],
            },
        )

    async def invoke() -> str:
        return await _ask_user_question_handler(
            {"questions": [{"prompt": "q"}]},
            session_id=session_id,
            session_store=store,
            tool_call_id=my_tool_id,
            lease_token=uuid4(),
        )

    result_raw, _ = await asyncio.gather(invoke(), chatter())
    result = json.loads(result_raw)
    assert result["cancelled"] is False
    assert result["responses"][0]["answer"] == "yes"


@pytest.mark.asyncio
async def test_handler_errors_without_session_context():
    # Missing store + session_id should produce an error payload, not a crash.
    raw = await _ask_user_question_handler(
        {"questions": [{"prompt": "q"}]},
    )
    payload = json.loads(raw)
    assert "error" in payload


@pytest.mark.asyncio
async def test_handler_reports_schema_error_as_json():
    raw = await _ask_user_question_handler(
        {"questions": []},
        session_id=uuid4(),
        session_store=FakeSessionStore(),
        tool_call_id="tc",
    )
    payload = json.loads(raw)
    assert "error" in payload


@pytest.mark.asyncio
async def test_handler_exits_quickly_when_session_already_paused():
    """An ask_user_question call made on an already-paused session must not hang --
    the first poll sees the paused status and returns ``cancelled``.
    """
    session_id = uuid4()
    store = FakeSessionStore(status="paused")

    raw = await asyncio.wait_for(
        _ask_user_question_handler(
            {"questions": [{"prompt": "q"}]},
            session_id=session_id,
            session_store=store,
            tool_call_id="tc",
            lease_token=uuid4(),
        ),
        timeout=5.0,
    )
    result = json.loads(raw)
    assert result["cancelled"] is True
    assert result["reason"] == "session.paused"
