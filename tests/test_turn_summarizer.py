"""Summary output must come from the answer, with reasoning disabled."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.harness.turn_summarizer import TurnArtifact, TurnSummarizer


def response(content=None, **extra):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, **extra), finish_reason="length",
    )])


def summarizer(create, *, managed=True):
    client = SimpleNamespace(
        base_url="http://localhost:8888/proxy/services/_summary_llm/agents/agent-1" if managed else "",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return TurnSummarizer(
        base_client=client, base_model="test",
        summary_client=client, summary_model="@preset/summary",
    )


async def caption(s):
    return await s.summarize_iteration(
        iteration_id="turn-1:0", reasoning="Searched for today's weather",
        tool_calls=[], prior_iteration_summaries=[],
    )


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_reasoning_is_never_used_as_a_caption_or_recap(field, caplog):
    # Even reasoning that happens to be valid caption JSON stays private.
    private = '{"caption":"Private reasoning accidentally shaped like a caption"}'
    s = summarizer(AsyncMock(return_value=response(**{field: private})))
    assert await caption(s) is None
    assert await s.summarize_turn(
        turn_id="turn-1", user_message="Weather?",
        iteration_summaries=["Searched the forecast"], artifacts=[],
    ) is None
    assert private not in caplog.text
    assert "summary returned no content" in caplog.text
    assert "finish_reason=length" in caplog.text
    assert "discarding malformed" not in caplog.text


async def test_short_summary_calls_disable_reasoning():
    create = AsyncMock(side_effect=[
        response('{"caption":"Found the Bucharest forecast"}'),
        response("Found today's weather."),
        response("report.pdf"),
    ])
    s = summarizer(create)
    assert await caption(s) == "Found the Bucharest forecast"
    recap = await s.summarize_turn(
        turn_id="turn-1", user_message="Weather?",
        iteration_summaries=["Found the forecast"], artifacts=[],
    )
    assert recap.recap == "Found today's weather."
    files = [TurnArtifact(kind="file", label=name, ref=name) for name in ("report.pdf", "scratch.txt")]
    assert await s.pick_deliverables(turn_id="turn-1", user_message="Make a report", artifacts=files) == files[:1]
    for call in create.await_args_list:
        assert call.kwargs["extra_body"] == {"reasoning": {"enabled": False}}


class RejectedParameter(Exception):
    status_code = 400


async def test_reasoning_control_rejection_keeps_json_mode_and_budget():
    create = AsyncMock(side_effect=[
        RejectedParameter("Unsupported parameter: reasoning"),
        response('{"caption":"Found the Bucharest forecast"}'),
    ])
    s = summarizer(create)
    assert await caption(s) == "Found the Bucharest forecast"
    retry = create.await_args_list[1].kwargs
    assert "extra_body" not in retry
    assert retry["response_format"] == {"type": "json_object"}
    assert retry["max_tokens"] == 96
    assert s._iteration_json_mode is True


async def test_json_mode_fallback_still_disables_reasoning():
    create = AsyncMock(side_effect=[
        RejectedParameter("Unsupported response_format"),
        response("Found the Bucharest forecast"),
    ])
    assert await caption(summarizer(create)) == "Found the Bucharest forecast"
    retry = create.await_args_list[1].kwargs
    assert "response_format" not in retry
    assert retry["extra_body"] == {"reasoning": {"enabled": False}}


async def test_summary_reads_text_content_parts():
    create = AsyncMock(return_value=response([
        {"type": "text", "text": '{"caption":"Found the Bucharest forecast"}'},
    ], reasoning="Working notes"))
    assert await caption(summarizer(create, managed=False)) == "Found the Bucharest forecast"


async def test_mixed_iteration_caption_omits_the_step_marker():
    # A skill_step call alongside real work still gets a caption, but the
    # marker adds nothing worth telling the caption model about.
    create = AsyncMock(return_value=response('{"caption":"Listed the directory"}'))
    s = summarizer(create)
    await s.summarize_iteration(
        iteration_id="turn-1:0", reasoning="",
        tool_calls=[
            {"id": "c1", "function": {
                "name": "skill_step",
                "arguments": '{"skill":"x","step":"s1","status":"started"}',
            }},
            {"id": "c2", "function": {"name": "terminal", "arguments": '{"command":"ls"}'}},
        ],
        tool_results=[
            {"tool_call_id": "c1", "content": '{"ok": true}'},
            {"tool_call_id": "c2", "content": "file1\nfile2"},
        ],
        prior_iteration_summaries=[],
    )
    sent = create.await_args.kwargs["messages"][1]["content"]
    assert "terminal" in sent
    assert "skill_step" not in sent


async def test_a_failed_marker_alone_is_still_captioned():
    create = AsyncMock(return_value=response('{"caption": "The step marker was rejected"}'))
    s = summarizer(create)
    calls = [{"id": "c1", "function": {"name": "skill_step", "arguments": '{"skill": "proc", "step": "s9", "status": "started"}'}}]
    results = [{"tool_call_id": "c1", "name": "skill_step", "content": '{"error": "step must be the id from the step heading, such as s3."}'}]
    out = await s.summarize_iteration(
        iteration_id="turn-1:0", reasoning="", tool_calls=calls, tool_results=results, prior_iteration_summaries=[],
    )
    assert out is not None
    assert create.await_count == 1
