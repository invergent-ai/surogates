"""Romanian comma-below letters at the harness call site.

Drives the real ``AgentHarness._run_loop`` / ``_request_final_summary``
bodies with scripted model replies (the same scaffolding as
``tests/test_loop_turn_id.py``) and checks what lands in the stored
``llm.response`` and ``llm.thinking`` events: the reply's prose is fixed;
tool-call arguments, reasoning and user messages are not.  Also the OpenAI
stream catch-up, which compares streamed text with the stored text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.api.routes.openai import _LiveText
from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType

from tests.test_loop_turn_id import _make_loop_harness, _make_session

RO_RAW = (
    "Am verificat fişierul şi am găsit două erori; le-am corectat aşa cum "
    "aţi cerut, iar raportul este gata pentru echipă."
)
RO_FIXED = (
    "Am verificat fișierul și am găsit două erori; le-am corectat așa cum "
    "ați cerut, iar raportul este gata pentru echipă."
)
TR_RAW = (
    "Dosyayı kontrol ettim ve iki hata buldum; şimdi düzelttim, rapor "
    "ekip için hazır. Teşekkürler!"
)
USAGE_STOP = {"model": "test-model", "finish_reason": "stop", "input_tokens": 1, "output_tokens": 2}


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    config: dict[str, Any] | None = None,
    user_text: str = "Verifică fişierul, te rog.",
) -> tuple[list[tuple[Any, dict]], list[dict]]:
    """Run one ``_run_loop`` over *replies*; return (emits, messages)."""
    pending = iter(replies)

    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        return next(pending)

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store, budget=IterationBudget(max_total=5))
    session = _make_session()
    if config is not None:
        session = session.model_copy(update={"config": config})
    messages: list[dict] = [{"role": "user", "content": user_text}]
    await harness._run_loop(
        session, messages, "system", SimpleNamespace(lease_token=uuid4()), all_events=[],
    )
    emits = [(c.args[1], c.args[2]) for c in store.emit_event.await_args_list]
    return emits, messages


def _of(emits: list[tuple[Any, dict]], kind: EventType) -> list[dict]:
    return [payload for event_type, payload in emits if event_type == kind]


@pytest.mark.asyncio
async def test_final_romanian_reply_is_stored_fixed_and_reasoning_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoning = "Utilizatorul vrea să verific fişierul; aşa că îl deschid şi caut erori."
    emits, messages = await _run(monkeypatch, [(
        {"role": "assistant", "content": RO_RAW, "reasoning": reasoning, "tool_calls": None},
        USAGE_STOP,
    )])

    [response] = _of(emits, EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_FIXED
    assert response["romanian_comma_below"] == 4
    [thinking] = _of(emits, EventType.LLM_THINKING)
    assert thinking["reasoning"] == reasoning
    assert response["message"].get("reasoning") == reasoning
    # The loop's own history carries the same text the event does.
    assert messages[-1]["content"] == RO_FIXED
    # The user's message is not touched.
    assert messages[0]["content"] == "Verifică fişierul, te rog."
    _of(emits, EventType.LLM_REQUEST)  # sanity: the request was emitted
    assert all("ş" not in p["message"]["content"] for p in _of(emits, EventType.LLM_RESPONSE))


@pytest.mark.asyncio
async def test_inline_thinking_is_extracted_unfixed_and_prose_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    think = "Trebuie să răspund în română şi să fiu scurt."
    emits, _ = await _run(monkeypatch, [(
        {"role": "assistant", "content": f"<think>{think}</think>{RO_RAW}", "tool_calls": None},
        USAGE_STOP,
    )])

    [thinking] = _of(emits, EventType.LLM_THINKING)
    assert think in thinking["reasoning"]
    [response] = _of(emits, EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_FIXED


@pytest.mark.asyncio
async def test_tool_call_arguments_keep_their_cedillas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply with tool calls is stored through the same call site: its
    prose is fixed, the arguments (here unparseable, so the loop persists
    the reply and asks again) are stored exactly as the model wrote them."""
    arguments = '{"path": "raport_ţară.txt", "text": "Aşa şi aşa'
    tool_reply = {
        "role": "assistant",
        "content": RO_RAW,
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "write_file", "arguments": arguments},
        }],
    }
    emits, _ = await _run(monkeypatch, [
        (tool_reply, {**USAGE_STOP, "finish_reason": "tool_calls"}),
        ({"role": "assistant", "content": TR_RAW, "tool_calls": None}, USAGE_STOP),
    ])

    first, second = _of(emits, EventType.LLM_RESPONSE)
    assert first["message"]["content"] == RO_FIXED
    assert first["message"]["tool_calls"][0]["function"]["arguments"] == arguments
    assert first["romanian_comma_below"] == 4
    # A Turkish reply in the same session passes through byte-identical.
    assert second["message"]["content"] == TR_RAW
    assert "romanian_comma_below" not in second


@pytest.mark.asyncio
async def test_switched_off_by_session_config(monkeypatch: pytest.MonkeyPatch) -> None:
    emits, _ = await _run(
        monkeypatch,
        [({"role": "assistant", "content": RO_RAW, "tool_calls": None}, USAGE_STOP)],
        config={"romanian_comma_below": False},
    )
    [response] = _of(emits, EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_RAW
    assert "romanian_comma_below" not in response


@pytest.mark.asyncio
async def test_final_summary_reply_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget-exhausted summary is the other place a model reply is stored."""
    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        return {"role": "assistant", "content": RO_RAW}, {}

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    store = AsyncMock()
    harness = _make_loop_harness(session_store=store)
    messages = [{"role": "user", "content": "Verifică fişierul."}]

    await harness._request_final_summary(
        _make_session(), messages, "system",
        SimpleNamespace(lease_token=uuid4()), request="Verifică fişierul.", reason="budget_exhausted",
    )

    response = next(c.args[2] for c in store.emit_event.await_args_list if c.args[1] == EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_FIXED
    assert response["romanian_comma_below"] == 4
    assert messages[0]["content"] == "Verifică fişierul."


# ---------------------------------------------------------------------------
# OpenAI-compatible stream: live frames carry the raw text, the stored
# response the fixed one.
# ---------------------------------------------------------------------------


def test_stream_catch_up_sends_the_rest_after_the_swap() -> None:
    """A stream that heard the first part of a call live, then lost frames,
    sends the rest from the stored response.  The stored text differs from
    the frames only by the swap, so the rest is still found and sent."""
    live = _LiveText(start=0)
    sent = RO_RAW[:40]  # "Am verificat fişierul şi am găsit două er"
    assert live._rest("content", sent, RO_FIXED) == [("content", RO_FIXED[40:])]


def test_stream_catch_up_still_refuses_a_different_text() -> None:
    live = _LiveText(start=0)
    assert live._rest("content", "Cu totul altceva", RO_FIXED) == []
    assert live._rest("content", RO_FIXED, RO_FIXED) == []
