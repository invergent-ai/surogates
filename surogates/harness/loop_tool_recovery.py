"""Tool-call validation and recovery helpers for the harness loop."""

from __future__ import annotations

import json
from typing import Any

def _is_valid_json_args(tc: dict) -> bool:
    """Check if a tool call's arguments are valid JSON."""
    import json as _json

    fn = tc.get("function", {})
    args_raw = fn.get("arguments", "")
    if not args_raw or not isinstance(args_raw, str):
        return True  # empty or already parsed — not invalid JSON
    args_raw = args_raw.strip()
    if not args_raw or args_raw == "{}":
        return True
    try:
        parsed = _json.loads(args_raw)
        return isinstance(parsed, dict)
    except (ValueError, TypeError):
        return False


def _canonical_args(raw: Any) -> str | None:
    """Canonicalise tool-call arguments for identity comparison."""
    if isinstance(raw, dict):
        parsed: Any = raw
    elif isinstance(raw, str):
        if not raw.strip():
            parsed = {}
        else:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return None
    else:
        return None
    if not isinstance(parsed, dict):
        return None
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _round_identity(messages: list[dict], index: int) -> tuple[str, str, str] | None:
    """Identity of the single-tool-call round starting at *index*, or None.

    A round is an assistant message with exactly one tool call followed
    immediately by its tool result. Identity is (name, canonical args,
    result content) — only true no-progress rounds compare equal.
    """
    if index >= len(messages):
        return None
    assistant = messages[index]
    if assistant.get("role") != "assistant":
        return None
    tool_calls = assistant.get("tool_calls") or []
    if len(tool_calls) != 1:
        return None
    if index + 1 >= len(messages):
        return None
    result = messages[index + 1]
    if result.get("role") != "tool":
        return None
    if result.get("tool_call_id") != tool_calls[0].get("id"):
        return None
    function = tool_calls[0].get("function") or {}
    args = _canonical_args(function.get("arguments", ""))
    if args is None:
        return None
    content = result.get("content")
    if not isinstance(content, str):
        return None
    return (function.get("name", ""), args, content)


def collapse_repeated_tool_rounds(messages: list[dict]) -> list[dict]:
    """Collapse runs of >2 identical no-progress tool rounds in a history.

    Providers reject whole conversations whose history repeats the same
    tool call with identical arguments across consecutive rounds (e.g.
    DashScope's ``400 Repetitive tool calls detected``), which makes a
    session that looped before failing impossible to resume. For each run
    of more than two consecutive identical (call, result) rounds, keep the
    first and last rounds, drop the middle, and annotate the surviving
    last result with an elision note — which also makes the two remaining
    results non-identical.
    """
    repaired: list[dict] = []
    i = 0
    while i < len(messages):
        identity = _round_identity(messages, i)
        if identity is None:
            repaired.append(messages[i])
            i += 1
            continue
        run_length = 1
        while _round_identity(messages, i + 2 * run_length) == identity:
            run_length += 1
        if run_length <= 2:
            repaired.extend(messages[i:i + 2 * run_length])
        else:
            last = i + 2 * (run_length - 1)
            note = (
                f"\n\n[Note: this exact tool call ran {run_length} times in "
                f"a row with an identical result; {run_length - 2} identical "
                "rounds were elided from the conversation history. Do not "
                "repeat this call — change the arguments or use a different "
                "approach.]"
            )
            repaired.extend(messages[i:i + 2])
            repaired.append(messages[last])
            repaired.append({
                **messages[last + 1],
                "content": messages[last + 1]["content"] + note,
            })
        i += 2 * run_length
    return repaired


def build_partial_tool_call_recovery_results(tool_calls: list[dict]) -> list[dict]:
    """Build model-visible tool results for truncated tool-call arguments."""
    results: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        tool_name = fn.get("name", "")
        results.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(
                    {
                        "error": (
                            "Partial tool call arguments detected. The provider "
                            "ended the response before the JSON arguments were "
                            "complete. Retry this tool call with complete JSON."
                        ),
                        "tool": tool_name,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    return results
