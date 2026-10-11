"""Functional coverage for ask_user_question answer and cancellation workflows.

Exercises asynchronous replies, tool-call matching, session pause and handler errors."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import surogates.tools.builtin.ask_user_question as ask_module
from surogates.session.events import EventType
from surogates.tools.builtin.ask_user_question import (
    _ask_user_question_handler,
    _wait_for_response,
)


class FakeEvent:
    """Minimal event shape the ask_user_question handler touches."""

    __slots__ = ("id", "session_id", "type", "data")

    def __init__(self, id_: int, session_id: Any, type_: str, data: dict[str, Any]) -> None:
        self.id = id_
        self.session_id = session_id
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
        self._events.append(FakeEvent(self._next_id, session_id, type_str, data))
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
            if ev.session_id != session_id or ev.id <= after:
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


async def _asked_while(store: FakeSessionStore, *said: dict, session_id: Any = None) -> dict:
    """The question's outcome when its user sends *said*, each a message's data, while it waits; then answers it."""
    session_id = session_id or uuid4()

    async def the_user() -> None:
        for data in said:
            await asyncio.sleep(0.05)
            await store.emit_event(session_id, EventType.USER_MESSAGE, data)
        # Long after the wait has looked again.
        await asyncio.sleep(0.2)
        await store.emit_event(session_id, EventType.ASK_USER_QUESTION_RESPONSE, {
            "tool_call_id": "tc", "responses": [{"question": "Which quarter?", "answer": "Q3", "is_other": False}],
        })

    raw, _ = await asyncio.wait_for(asyncio.gather(
        _ask_user_question_handler(
            {"questions": [{"prompt": "Which quarter?"}]},
            session_id=session_id, session_store=store, tool_call_id="tc", lease_token=uuid4(),
        ),
        the_user(),
    ), timeout=5.0)
    return json.loads(raw)


@pytest.fixture
def looks_often(monkeypatch):
    """The wait looks at the log every 10 ms, as a nudge would have it look at once."""
    monkeypatch.setattr(ask_module, "_POLL_INTERVAL_SECONDS", 0.01)


@pytest.mark.asyncio
async def test_a_command_typed_while_the_question_waits_dismisses_it(looks_often):
    result = await _asked_while(FakeSessionStore(), {"content": "/goal status"})
    # Not answered, and the model is told why, with what it asked: it may ask again.
    assert result == {
        "cancelled": True, "reason": "dismissed", "detail": ask_module.DISMISSED_BY_A_COMMAND,
        "questions_asked": [{"prompt": "Which quarter?", "allow_other": True}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("said", [
    {"content": "Use the Q3 figures."},
    # The harness's own messages are no command of its user's, whatever their words.
    {"content": "/goal status", "synthetic": "outcome_continuation"},
], ids=["a plain message", "the harness's message"])
async def test_only_a_command_its_user_typed_dismisses_the_question(looks_often, said):
    result = await _asked_while(FakeSessionStore(), said)
    assert (result["cancelled"], result["responses"][0]["answer"]) == (False, "Q3")


class LandsAfterARead(FakeSessionStore):
    """A store in which *then*, each an event's type and data, is written right after the wait's first look at the log."""

    def __init__(self, then: list[tuple[EventType, dict]]) -> None:
        super().__init__()
        self._then = then

    async def get_events(self, session_id: Any, **kwargs: Any) -> list[FakeEvent]:
        events = await super().get_events(session_id, **kwargs)
        asked = any(event.type == EventType.INBOX_INPUT_REQUIRED.value for event in self._events)
        if asked and self._then:
            then, self._then = self._then, []
            for type_, data in then:
                await self.emit_event(session_id, type_, data)
        return events


ANSWERED = (EventType.ASK_USER_QUESTION_RESPONSE, {
    "tool_call_id": "tc", "responses": [{"question": "Which quarter?", "answer": "Q3", "is_other": False}],
})
COMMAND = (EventType.USER_MESSAGE, {"content": "/goal status"})


@pytest.mark.asyncio
@pytest.mark.parametrize("then", [[ANSWERED, COMMAND], [COMMAND, ANSWERED]], ids=["answered first", "the command first"])
async def test_an_answer_the_wait_finds_with_a_command_is_never_lost_for_it(looks_often, then):
    session_id = uuid4()
    raw = await asyncio.wait_for(_ask_user_question_handler(
        {"questions": [{"prompt": "Which quarter?"}]},
        session_id=session_id, session_store=LandsAfterARead(then), tool_call_id="tc", lease_token=uuid4(),
    ), timeout=5.0)
    # Its route told its user the answer was taken: the model reads it, and the command waits for the turn's end.
    result = json.loads(raw)
    assert (result["cancelled"], result["responses"][0]["answer"]) == (False, "Q3")


@pytest.mark.asyncio
async def test_a_command_typed_before_the_question_was_asked_does_not_dismiss_it(looks_often):
    session_id, store = uuid4(), FakeSessionStore()
    await store.emit_event(session_id, EventType.USER_MESSAGE, {"content": "/goal status"})
    result = await _asked_while(store, session_id=session_id)
    assert (result["cancelled"], result["responses"][0]["answer"]) == (False, "Q3")


class NudgedStore(FakeSessionStore):
    """A store whose events wake every listener, like SessionStore's Redis nudge."""

    def __init__(self) -> None:
        super().__init__()
        self.nudge = asyncio.Event()

    async def emit_event(self, session_id, type_, data) -> int:
        event_id = await super().emit_event(session_id, type_, data)
        self.nudge.set()
        return event_id


class FakePubSub:
    def __init__(self, store: NudgedStore, *, delivers: bool = True) -> None:
        self.store = store
        self.delivers = delivers
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        if not self.delivers:
            await asyncio.sleep(timeout)
            return None
        try:
            await asyncio.wait_for(self.store.nudge.wait(), timeout)
        except TimeoutError:
            return None
        self.store.nudge.clear()
        return {"type": "message", "data": b"1:ask_user_question.response"}

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> FakePubSub:
        return self._pubsub


async def _answer(store, session_id, tool_call_id, *, after: float) -> None:
    await asyncio.sleep(after)
    await store.emit_event(session_id, EventType.ASK_USER_QUESTION_RESPONSE, {
        "tool_call_id": tool_call_id,
        "responses": [{"question": "q", "answer": "A", "is_other": False}],
    })


@pytest.mark.asyncio
async def test_a_reply_wakes_the_wait_at_once(monkeypatch):
    monkeypatch.setattr(ask_module, "_POLL_INTERVAL_SECONDS", 30)  # polling alone would take 30 s
    session_id, store = uuid4(), NudgedStore()
    pubsub = FakePubSub(store)
    outcome, _ = await asyncio.wait_for(asyncio.gather(
        _wait_for_response(
            session_id=session_id, tool_call_id="call_1", session_store=store,
            lease_token=None, redis=FakeRedis(pubsub),
        ),
        _answer(store, session_id, "call_1", after=0.05),
    ), 2.0)
    assert outcome["cancelled"] is False
    assert pubsub.subscribed == [f"surogates:session:{session_id}"]
    assert pubsub.closed


@pytest.mark.asyncio
async def test_a_lost_nudge_is_caught_by_the_recheck(monkeypatch):
    monkeypatch.setattr(ask_module, "_POLL_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(ask_module, "_RECHECK_INTERVAL_SECONDS", 0.1, raising=False)
    session_id, store = uuid4(), NudgedStore()
    outcome, _ = await asyncio.wait_for(asyncio.gather(
        _wait_for_response(
            session_id=session_id, tool_call_id="call_1", session_store=store,
            lease_token=None, redis=FakeRedis(FakePubSub(store, delivers=False)),
        ),
        _answer(store, session_id, "call_1", after=0.05),
    ), 2.0)
    assert outcome["cancelled"] is False


@pytest.mark.asyncio
async def test_the_cap_still_ends_a_nudged_wait(monkeypatch):
    monkeypatch.setattr(ask_module, "_MAX_WAIT_SECONDS", 0.2)
    session_id, store = uuid4(), NudgedStore()
    outcome = await asyncio.wait_for(_wait_for_response(
        session_id=session_id, tool_call_id="call_1", session_store=store,
        lease_token=None, redis=FakeRedis(FakePubSub(store)),
    ), 2.0)
    assert outcome == {"cancelled": True, "reason": "timeout"}


@pytest.mark.asyncio
async def test_without_redis_the_wait_still_polls(monkeypatch):
    monkeypatch.setattr(ask_module, "_POLL_INTERVAL_SECONDS", 0.01)
    session_id, store = uuid4(), FakeSessionStore()
    outcome, _ = await asyncio.wait_for(asyncio.gather(
        _wait_for_response(
            session_id=session_id, tool_call_id="call_1", session_store=store, lease_token=None,
        ),
        _answer(store, session_id, "call_1", after=0.05),
    ), 2.0)
    assert outcome["cancelled"] is False


class BrokenPubSub(FakePubSub):
    async def subscribe(self, channel: str) -> None:
        raise ConnectionError("redis is down")


class HangingPubSub(FakePubSub):
    async def subscribe(self, channel: str) -> None:
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_a_failed_subscription_falls_back_to_polling(monkeypatch):
    monkeypatch.setattr(ask_module, "_POLL_INTERVAL_SECONDS", 0.01)
    session_id, store = uuid4(), NudgedStore()
    pubsub = BrokenPubSub(store)
    outcome, _ = await asyncio.wait_for(asyncio.gather(
        _wait_for_response(
            session_id=session_id, tool_call_id="call_1", session_store=store,
            lease_token=None, redis=FakeRedis(pubsub),
        ),
        _answer(store, session_id, "call_1", after=0.05),
    ), 2.0)
    assert outcome["cancelled"] is False
    assert pubsub.closed


@pytest.mark.asyncio
async def test_a_cancel_during_subscribe_closes_the_pubsub():
    session_id, store = uuid4(), NudgedStore()
    pubsub = HangingPubSub(store)
    wait = asyncio.create_task(_wait_for_response(
        session_id=session_id, tool_call_id="call_1", session_store=store,
        lease_token=None, redis=FakeRedis(pubsub),
    ))
    await asyncio.sleep(0.05)
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert pubsub.closed
