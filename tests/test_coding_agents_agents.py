"""Unit tests for coding-agent command builders + stream-json parsing."""

from __future__ import annotations

import json

import pytest

from surogates.coding_agents.agents import (
    CodeInvocation,
    CodeResult,
    build_invocation,
    parse_stream,
)


def test_build_claude_default_is_bypass_with_prompt_on_stdin():
    inv = build_invocation("claude", "fix the build")
    assert isinstance(inv, CodeInvocation)
    assert inv.argv[0] == "claude"
    assert "-p" in inv.argv
    assert "--output-format" in inv.argv
    assert inv.argv[inv.argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in inv.argv
    # Default (not read-only) runs in bypass mode.
    assert "--dangerously-skip-permissions" in inv.argv
    # Prompt travels on stdin, never in argv (avoids quoting + log leakage).
    assert inv.stdin == "fix the build"
    assert "fix the build" not in inv.argv


def test_build_claude_read_only_uses_plan_mode():
    inv = build_invocation("claude", "look around", read_only=True)
    assert "--dangerously-skip-permissions" not in inv.argv
    assert "--permission-mode" in inv.argv
    assert inv.argv[inv.argv.index("--permission-mode") + 1] == "plan"


def test_build_claude_model_and_effort():
    inv = build_invocation("claude", "go", model="opus", effort="high")
    assert inv.argv[inv.argv.index("--model") + 1] == "opus"
    assert inv.argv[inv.argv.index("--effort") + 1] == "high"


def test_build_codex_default_is_bypass_with_positional_prompt():
    inv = build_invocation("codex", "ship it")
    assert inv.argv[0] == "codex"
    assert inv.argv[1] == "exec"
    assert "--json" in inv.argv
    assert "--skip-git-repo-check" in inv.argv
    assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv
    # Codex takes the prompt positionally as the final argv element.
    assert inv.argv[-1] == "ship it"
    assert inv.stdin is None


def test_build_codex_read_only_sandbox():
    inv = build_invocation("codex", "read only", read_only=True)
    assert "--dangerously-bypass-approvals-and-sandbox" not in inv.argv
    assert "--sandbox" in inv.argv
    assert inv.argv[inv.argv.index("--sandbox") + 1] == "read-only"


def test_build_codex_effort_via_config_override():
    inv = build_invocation("codex", "go", effort="xhigh")
    assert "-c" in inv.argv
    assert "model_reasoning_effort=xhigh" in inv.argv


def test_build_rejects_unknown_agent_and_effort():
    with pytest.raises(ValueError, match="agent"):
        build_invocation("gemini", "x")
    with pytest.raises(ValueError, match="effort"):
        build_invocation("claude", "x", effort="turbo")


def test_build_rejects_empty_prompt():
    with pytest.raises(ValueError, match="prompt"):
        build_invocation("claude", "   ")


def test_parse_claude_stream_extracts_final_message_and_usage():
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Working on it"}]},
        }),
        "not json — tolerated",
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "Done. Fixed the build.",
            "usage": {"input_tokens": 1200, "output_tokens": 340},
        }),
    ]
    result = parse_stream("claude", "\n".join(lines))
    assert isinstance(result, CodeResult)
    assert result.final_message == "Done. Fixed the build."
    assert result.input_tokens == 1200
    assert result.output_tokens == 340
    assert result.error is None


def test_parse_claude_falls_back_to_last_assistant_text():
    # No explicit result event — fall back to the last assistant text.
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}}),
    ]
    result = parse_stream("claude", "\n".join(lines))
    assert result.final_message == "second"


def test_parse_codex_stream_extracts_message_and_tokens():
    lines = [
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "All set."}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 90, "output_tokens": 12}}),
    ]
    result = parse_stream("codex", "\n".join(lines))
    assert result.final_message == "All set."
    assert result.input_tokens == 90
    assert result.output_tokens == 12


def test_summarize_progress_is_readable_not_raw_json():
    from surogates.coding_agents.agents import summarize_progress

    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Creating the file"},
            {"type": "tool_use", "name": "Write"},
        ]}}),
        json.dumps({"type": "rate_limit_event"}),
        json.dumps({"type": "result", "result": "Done."}),
    ]
    out = summarize_progress("claude", "\n".join(lines))
    assert "Creating the file" in out
    assert "Write" in out
    # Bookkeeping + the terminal result are not echoed into progress.
    assert "rate_limit_event" not in out
    assert "Done." not in out
    assert "{" not in out  # no raw JSON


def test_summarize_progress_codex_agent_message():
    from surogates.coding_agents.agents import summarize_progress

    line = json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "all set"}})
    assert summarize_progress("codex", line) == "all set"


def test_parse_empty_stream_is_error_result():
    result = parse_stream("claude", "")
    assert result.final_message == ""
    assert result.error is not None


def test_parse_tolerates_all_garbage():
    result = parse_stream("codex", "garbage\nmore garbage\n{bad json")
    assert result.final_message == ""
    assert result.error is not None
