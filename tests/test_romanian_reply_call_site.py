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
from surogates.harness.romanian_diacritics import normalize_reply_text, sampled_content
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
    # The model's own text stays on the event for training / rewards.
    assert response["raw_content"] == RO_RAW
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
    assert "raw_content" not in second


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
    assert "raw_content" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, 0, "false", "0", "off", "No", " OFF "])
async def test_switched_off_by_any_off_value(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    emits, _ = await _run(
        monkeypatch,
        [({"role": "assistant", "content": RO_RAW, "tool_calls": None}, USAGE_STOP)],
        config={"romanian_comma_below": value},
    )
    [response] = _of(emits, EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_RAW


@pytest.mark.asyncio
async def test_worker_default_off_and_session_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker setting is the default; a session's own value wins."""
    from surogates.harness.loop import AgentHarness

    monkeypatch.setattr(AgentHarness, "_romanian_comma_below", False)
    emits, _ = await _run(
        monkeypatch, [({"role": "assistant", "content": RO_RAW, "tool_calls": None}, USAGE_STOP)],
    )
    assert _of(emits, EventType.LLM_RESPONSE)[0]["message"]["content"] == RO_RAW
    emits, _ = await _run(
        monkeypatch, [({"role": "assistant", "content": RO_RAW, "tool_calls": None}, USAGE_STOP)],
        config={"romanian_comma_below": "on"},
    )
    assert _of(emits, EventType.LLM_RESPONSE)[0]["message"]["content"] == RO_FIXED


@pytest.mark.asyncio
async def test_length_partials_are_stored_raw_and_the_joined_reply_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial reply cut at the length limit is stored as written (the
    next part may start inside its code block); the final event holds the
    joined reply, settled as a whole."""
    part1 = "Am verificat fişierul şi am găsit erori:\n```python\nx = 'şablon"
    part2 = "'\n```\nLe-am corectat aşa cum aţi cerut, iar raportul este gata pentru echipă."
    emits, _ = await _run(monkeypatch, [
        ({"role": "assistant", "content": part1, "tool_calls": None},
         {**USAGE_STOP, "finish_reason": "length"}),
        ({"role": "assistant", "content": part2, "tool_calls": None}, USAGE_STOP),
    ])
    first, final = _of(emits, EventType.LLM_RESPONSE)
    assert first["message"]["content"] == part1
    assert "raw_content" not in first
    joined = part1 + part2
    assert final["raw_content"] == joined
    assert final["message"]["content"] == (
        "Am verificat fișierul și am găsit erori:\n```python\nx = 'şablon"
        "'\n```\nLe-am corectat așa cum ați cerut, iar raportul este gata pentru echipă."
    )


class _StopAfterRescue(Exception):
    pass


@pytest.mark.asyncio
async def test_question_rescue_copies_the_fixed_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """On an end-user channel a question-shaped reply becomes an
    ``ask_user_question`` call whose arguments carry the reply's text and
    whose content is emptied.  The text it copies is already fixed, and the
    question (here quoted by the judge with cedillas) is still cut out."""
    from surogates.harness.loop import AgentHarness

    question = "Vreţi să trimit raportul echipei?"
    reply = RO_RAW + " " + question
    seen: dict[str, Any] = {}

    async def run_rescue_then_stop(self: Any, **kwargs: Any) -> str | None:
        kind = await AgentHarness._maybe_route_final_response_to_inbox(self, **kwargs)
        seen["kind"] = kind
        seen["message"] = kwargs["assistant_message"]
        raise _StopAfterRescue

    pending = iter([({"role": "assistant", "content": reply, "tool_calls": None}, USAGE_STOP)])

    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        return next(pending)

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools.tool_names = {"ask_user_question"}
    harness._governance_gate = None
    harness._judge_final_response_user_action = AsyncMock(return_value={
        "action_kind": "ask_user_question", "question": question,
    })
    harness._maybe_route_final_response_to_inbox = run_rescue_then_stop.__get__(harness)

    with pytest.raises(_StopAfterRescue):
        await harness._run_loop(
            _make_session(), [{"role": "user", "content": "Verifică fişierul."}], "system",
            SimpleNamespace(lease_token=uuid4()), all_events=[],
        )

    assert seen["kind"] == "ask_user_question"
    message = seen["message"]
    assert message["content"] is None
    import json
    arguments = json.loads(message["tool_calls"][0]["function"]["arguments"])
    assert arguments["context"] == RO_FIXED
    assert arguments["questions"][0]["prompt"] == question
    judged = harness._judge_final_response_user_action.await_args.kwargs
    assert judged["assistant_content"] == RO_FIXED + " " + question.replace("ţ", "ț")


@pytest.mark.asyncio
async def test_action_required_rescue_gets_the_fixed_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other rescue, an ``action_required`` inbox item, quotes the
    reply as its context: fixed too, and the reply is stored with its raw text."""
    from surogates.harness.loop import AgentHarness

    pending = iter([({"role": "assistant", "content": RO_RAW, "tool_calls": None}, USAGE_STOP)])

    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        return next(pending)

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._judge_final_response_user_action = AsyncMock(return_value={
        "action_kind": "action_required", "instructions": "Autentificaţi-vă.",
    })
    harness._maybe_route_final_response_to_inbox = (
        AgentHarness._maybe_route_final_response_to_inbox.__get__(harness)
    )

    await harness._run_loop(
        _make_session(), [{"role": "user", "content": "Verifică fişierul."}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=[],
    )

    emits = [(c.args[1], c.args[2]) for c in store.emit_event.await_args_list]
    [item] = _of(emits, EventType.INBOX_ACTION_REQUIRED)
    assert item["context"] == RO_FIXED
    [response] = _of(emits, EventType.LLM_RESPONSE)
    assert response["message"]["content"] == RO_FIXED
    assert response["raw_content"] == RO_RAW
    assert response["romanian_comma_below"] == 4


def test_training_export_uses_the_models_own_text() -> None:
    """Training export must carry the tokens the model sampled, not the
    harness's fix: ``raw_content`` when it differs from the stored text only
    by the swap, else the stored content."""
    from surogates.jobs.training_collector import TrainingDataCollector

    collector = TrainingDataCollector.__new__(TrainingDataCollector)
    skill_event = SimpleNamespace(
        id=1, type=EventType.SKILL_INVOKED.value,
        data={"skill": "raport", "raw_message": "/raport verifică fişierul"}, created_at=None,
    )
    fixed_reply = SimpleNamespace(id=2, type=EventType.LLM_RESPONSE.value, data={
        "message": {"role": "assistant", "content": RO_FIXED},
        "raw_content": RO_RAW, "romanian_comma_below": 4,
    })
    plain_reply = SimpleNamespace(id=3, type=EventType.LLM_RESPONSE.value, data={
        "message": {"role": "assistant", "content": "Gata."},
        # A stale raw text that is not this content: ignored.
        "raw_content": "Cu totul altceva.",
    })
    example, rejected = collector._collect_skill_trajectory(
        [skill_event, fixed_reply, plain_reply], 0, "raport", uuid4(), skill_event,
    )
    assert not rejected and example is not None
    assistant = [m for m in example.messages if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == [RO_RAW, "Gata."]
    assert all("raw_content" not in m for m in assistant)


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


@pytest.mark.asyncio
async def test_last_length_part_inside_a_continued_code_block_keeps_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last part of a reply cut at the length limit starts inside the
    code block the first part opened.  Settled alone it would read that code
    as prose; it is settled only joined, so the code keeps its letters and
    ``raw_content`` is the joined text exactly as the model wrote it."""
    part1 = "Am scris scriptul care şterge fişierele vechi:\n```python\nimport os\n"
    part2 = (
        "mesaj = 'Fişierele vechi au fost şterse şi sunt acum în coş pentru toţi'\n"
        "print(mesaj)\n```\nLe-am corectat aşa cum aţi cerut, iar raportul este gata."
    )
    emits, _ = await _run(monkeypatch, [
        ({"role": "assistant", "content": part1, "tool_calls": None},
         {**USAGE_STOP, "finish_reason": "length"}),
        ({"role": "assistant", "content": part2, "tool_calls": None}, USAGE_STOP),
    ])
    _first, final = _of(emits, EventType.LLM_RESPONSE)
    joined = part1 + part2
    assert final["raw_content"] == joined
    assert sampled_content(final) == joined
    assert final["message"]["content"] == normalize_reply_text(joined)[0]
    assert (
        "mesaj = 'Fişierele vechi au fost şterse şi sunt acum în coş pentru toţi'"
        in final["message"]["content"]
    )
    assert final["message"]["content"].startswith("Am scris scriptul care șterge fișierele vechi:")
    assert final["message"]["content"].endswith("Le-am corectat așa cum ați cerut, iar raportul este gata.")


class _StopAfterPersist(Exception):
    pass


@pytest.mark.asyncio
async def test_question_rescue_stores_no_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the rescue the stored reply has no content (its text is in the
    ``ask_user_question`` arguments, fixed), so it carries no
    ``raw_content`` and no count."""
    import json

    from surogates.harness.loop import AgentHarness

    question = "Vreți să trimit raportul echipei?"
    pending = iter([({"role": "assistant", "content": RO_RAW + " " + question, "tool_calls": None}, USAGE_STOP)])

    async def fake_call_llm_with_retry(**_kwargs: Any) -> tuple[dict, dict]:
        # The rescued reply is stored and its call answered before the next
        # model call, which ends the test.
        for reply in pending:
            return reply
        raise _StopAfterPersist

    monkeypatch.setattr("surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 200))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools.tool_names = {"ask_user_question"}
    harness._governance_gate = None
    harness._judge_final_response_user_action = AsyncMock(return_value={
        "action_kind": "ask_user_question", "question": question,
    })
    harness._maybe_route_final_response_to_inbox = (
        AgentHarness._maybe_route_final_response_to_inbox.__get__(harness)
    )

    with pytest.raises(_StopAfterPersist):
        await harness._run_loop(
            _make_session(), [{"role": "user", "content": "Verifică fişierul."}], "system",
            SimpleNamespace(lease_token=uuid4()), all_events=[],
        )

    emits = [(c.args[1], c.args[2]) for c in store.emit_event.await_args_list]
    response = _of(emits, EventType.LLM_RESPONSE)[0]
    assert response["message"]["tool_calls"][0]["function"]["name"] == "ask_user_question"
    assert response["message"]["content"] is None
    assert "raw_content" not in response and "romanian_comma_below" not in response
    arguments = json.loads(response["message"]["tool_calls"][0]["function"]["arguments"])
    assert arguments["context"] == RO_FIXED
