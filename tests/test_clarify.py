"""Tests for the clarify tool and its response endpoint.

Covers:
- Schema validation (``_validate_questions``).
- Handler round-trip with a fake session store (response arrives).
- Handler cancellation via session status flip to ``paused``.
- Handler ignores responses with a non-matching ``tool_call_id``.
- The ``ClarifyAnswer`` / ``ClarifyResponseRequest`` API models.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from surogates.api.routes.clarify import (
    ClarifyAnswer,
    ClarifyResponseRequest,
)
from surogates.session.events import EventType
from surogates.tools.builtin.clarify import (
    ClarifySchemaError,
    MAX_CHOICES_PER_QUESTION,
    MAX_QUESTIONS,
    _clarify_handler,
    _validate_questions,
)


# =========================================================================
# Fake event / store
# =========================================================================


class FakeEvent:
    """Minimal event shape the clarify handler touches."""

    __slots__ = ("id", "type", "data")

    def __init__(self, id_: int, type_: str, data: dict[str, Any]) -> None:
        self.id = id_
        self.type = type_
        self.data = data


class FakeSessionStore:
    """In-memory session store sufficient for the clarify handler.

    Implements ``emit_event``, ``get_events``, ``get_session``, and
    ``renew_lease`` -- every method the clarify handler reaches for.
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


# =========================================================================
# Schema validation
# =========================================================================


class TestValidateQuestions:

    def test_requires_array(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions("not an array")  # type: ignore[arg-type]

    def test_rejects_empty(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions([])

    def test_rejects_too_many(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions(
                [{"prompt": f"q{i}"} for i in range(MAX_QUESTIONS + 1)],
            )

    def test_rejects_missing_prompt(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions([{"choices": []}])

    def test_rejects_blank_prompt(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions([{"prompt": "   "}])

    def test_rejects_too_many_choices(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions([{
                "prompt": "pick",
                "choices": [
                    {"label": f"c{i}"} for i in range(MAX_CHOICES_PER_QUESTION + 1)
                ],
            }])

    def test_rejects_missing_choice_label(self):
        with pytest.raises(ClarifySchemaError):
            _validate_questions([{
                "prompt": "pick",
                "choices": [{"description": "only desc"}],
            }])

    def test_open_ended_question_allowed(self):
        out = _validate_questions([{"prompt": "describe"}])
        assert out == [{"prompt": "describe", "allow_other": True}]

    def test_normalises_full_question(self):
        out = _validate_questions([
            {
                "prompt": "  Pick one  ",
                "choices": [
                    {"label": "A", "description": "first"},
                    {"label": "B"},
                ],
                "allow_other": False,
            },
        ])
        assert out == [{
            "prompt": "Pick one",
            "allow_other": False,
            "choices": [
                {"label": "A", "description": "first"},
                {"label": "B"},
            ],
        }]

    def test_allow_other_defaults_true(self):
        out = _validate_questions([{"prompt": "x"}])
        assert out[0]["allow_other"] is True

    def test_drops_non_string_description(self):
        out = _validate_questions([{
            "prompt": "x",
            "choices": [{"label": "L", "description": 42}],
        }])
        assert out[0]["choices"] == [{"label": "L"}]

    def test_caps_prompt_length(self):
        very_long = "a" * 10000
        out = _validate_questions([{"prompt": very_long}])
        assert len(out[0]["prompt"]) <= 1000


# =========================================================================
# Handler round-trip
# =========================================================================


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
            EventType.CLARIFY_RESPONSE,
            {
                "tool_call_id": tool_call_id,
                "responses": [
                    {"question": "Pick one", "answer": "A", "is_other": False},
                ],
            },
        )

    async def invoke() -> str:
        return await _clarify_handler(
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
        return await _clarify_handler(
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
            EventType.CLARIFY_RESPONSE,
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
            EventType.CLARIFY_RESPONSE,
            {
                "tool_call_id": my_tool_id,
                "responses": [
                    {"question": "q", "answer": "yes", "is_other": False},
                ],
            },
        )

    async def invoke() -> str:
        return await _clarify_handler(
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
    raw = await _clarify_handler(
        {"questions": [{"prompt": "q"}]},
    )
    payload = json.loads(raw)
    assert "error" in payload


@pytest.mark.asyncio
async def test_handler_reports_schema_error_as_json():
    raw = await _clarify_handler(
        {"questions": []},
        session_id=uuid4(),
        session_store=FakeSessionStore(),
        tool_call_id="tc",
    )
    payload = json.loads(raw)
    assert "error" in payload


@pytest.mark.asyncio
async def test_handler_exits_quickly_when_session_already_paused():
    """A clarify call made on an already-paused session must not hang --
    the first poll sees the paused status and returns ``cancelled``.
    """
    session_id = uuid4()
    store = FakeSessionStore(status="paused")

    raw = await asyncio.wait_for(
        _clarify_handler(
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


# =========================================================================
# Endpoint model validation
# =========================================================================


class TestClarifyRequestModel:

    def test_answer_requires_question_and_answer(self):
        with pytest.raises(ValidationError):
            ClarifyAnswer(question="", answer="a")
        with pytest.raises(ValidationError):
            ClarifyAnswer(question="q", answer="")

    def test_strips_whitespace(self):
        a = ClarifyAnswer(question="  q  ", answer=" a ")
        assert a.question == "q"
        assert a.answer == "a"

    def test_is_other_defaults_false(self):
        a = ClarifyAnswer(question="q", answer="a")
        assert a.is_other is False

    def test_request_requires_at_least_one_response(self):
        with pytest.raises(ValidationError):
            ClarifyResponseRequest(responses=[])

    def test_request_caps_responses(self):
        too_many = [
            ClarifyAnswer(question=f"q{i}", answer=f"a{i}")
            for i in range(MAX_QUESTIONS + 1)
        ]
        with pytest.raises(ValidationError):
            ClarifyResponseRequest(responses=too_many)

    def test_answer_length_capped(self):
        with pytest.raises(ValidationError):
            ClarifyAnswer(question="q", answer="a" * 5000)
