"""Tests for tool-call recovery helpers, including poisoned-history repair.

``collapse_repeated_tool_rounds`` regression context: a production session
accumulated 5 consecutive identical ``create_artifact`` rounds (same empty
arguments, same failure result). The provider then rejected *every*
subsequent request over that history with ``400 Repetitive tool calls
detected``, so user resumes could never recover. Rebuilt histories must
collapse such runs before they are sent back to the provider.
"""

from __future__ import annotations

import json

from surogates.harness.loop_tool_recovery import collapse_repeated_tool_rounds


def _round(call_id: str, name: str = "create_artifact", args: str = "{}",
           result: str = '{"success": false, "error": "name and kind are required."}',
           content: str | None = None) -> list[dict]:
    """Build one assistant tool-call round (assistant msg + tool result)."""
    return [
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def test_collapses_run_of_identical_failed_rounds() -> None:
    messages = [{"role": "user", "content": "make a brand book"}]
    for i in range(5):
        messages += _round(f"call_{i}")
    messages.append({"role": "assistant", "content": "giving up"})

    repaired = collapse_repeated_tool_rounds(messages)

    # 5 rounds collapse to 2 (first + last): 1 user + 2*2 + 1 assistant.
    assert len(repaired) == 6
    assert repaired[0]["role"] == "user"
    assert repaired[1]["tool_calls"][0]["id"] == "call_0"
    assert repaired[3]["tool_calls"][0]["id"] == "call_4"
    assert repaired[-1]["content"] == "giving up"
    # The surviving last result is annotated with the elision note, which
    # also makes it non-identical to the first result.
    note = repaired[4]["content"]
    assert "5 times" in note
    assert "elided" in note
    assert repaired[2]["content"] != repaired[4]["content"]


def test_short_runs_left_untouched() -> None:
    messages = [{"role": "user", "content": "hi"}]
    for i in range(2):
        messages += _round(f"call_{i}")

    assert collapse_repeated_tool_rounds(messages) == messages


def test_different_arguments_break_the_run() -> None:
    messages = []
    for i in range(5):
        messages += _round(f"call_{i}", args=json.dumps({"attempt": i}))

    assert collapse_repeated_tool_rounds(messages) == messages


def test_different_results_break_the_run() -> None:
    messages = []
    for i in range(5):
        messages += _round(f"call_{i}", result=json.dumps({"error": f"e{i}"}))

    assert collapse_repeated_tool_rounds(messages) == messages


def test_multi_tool_call_assistant_messages_untouched() -> None:
    call = {
        "id": "call_a",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    messages = []
    for i in range(4):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {**call, "id": f"call_a{i}"},
                {**call, "id": f"call_b{i}"},
            ],
        })
        messages.append({"role": "tool", "tool_call_id": f"call_a{i}", "content": "{}"})
        messages.append({"role": "tool", "tool_call_id": f"call_b{i}", "content": "{}"})

    assert collapse_repeated_tool_rounds(messages) == messages


def test_two_separate_runs_both_collapse() -> None:
    messages = []
    for i in range(3):
        messages += _round(f"call_x{i}", name="tool_x")
    messages.append({"role": "user", "content": "between"})
    for i in range(4):
        messages += _round(f"call_y{i}", name="tool_y")

    repaired = collapse_repeated_tool_rounds(messages)

    # 3 → 2 rounds and 4 → 2 rounds, plus the user message.
    assert len(repaired) == 2 * 2 + 1 + 2 * 2
    assert "3 times" in repaired[3]["content"]
    assert "4 times" in repaired[-1]["content"]


def test_equivalent_args_with_different_key_order_collapse() -> None:
    messages = []
    for i in range(4):
        args = '{"a": 1, "b": 2}' if i % 2 == 0 else '{"b": 2, "a": 1}'
        messages += _round(f"call_{i}", args=args)

    repaired = collapse_repeated_tool_rounds(messages)

    assert len(repaired) == 4  # two rounds kept
    assert "4 times" in repaired[-1]["content"]


def test_rebuild_messages_collapses_poisoned_history() -> None:
    """The wake-time replay must repair a history poisoned by a prior
    identical-call loop, so a resumed session can get past provider-side
    repetition guards."""
    from types import SimpleNamespace

    from surogates.harness.loop_context_replay import ContextReplayMixin

    events = [SimpleNamespace(
        type="user.message", data={"content": "make a brand book"},
    )]
    for i in range(5):
        events.append(SimpleNamespace(
            type="llm.response",
            data={"message": _round(f"call_{i}")[0]},
        ))
        events.append(SimpleNamespace(
            type="tool.result",
            data={
                "tool_call_id": f"call_{i}",
                "content": '{"success": false, "error": "name and kind are required."}',
            },
        ))

    harness = type("H", (ContextReplayMixin,), {})()
    messages = harness._rebuild_messages(events)

    assert len(messages) == 5  # user + 2 surviving rounds
    assert "elided" in messages[-1]["content"]
