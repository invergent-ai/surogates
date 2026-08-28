"""Deterministic failure detectors over a stored rollout trace.

Cheap and unambiguous. Anything these catch never reaches the LLM
attribution stage, which keeps that stage focused on the genuinely hard
residue: every tool worked and the answer is still wrong.
"""
from __future__ import annotations

from typing import Any

from gaia_bench.runner import RolloutResult

# The harness's explicit failure envelope. Matching looser prose ("error:",
# "traceback") flags tool output that merely DISCUSSES an error -- a fetched
# GitHub bug report, a log excerpt, a stack trace in a web page. That is
# data, not a failure, and it biases the fix list toward whichever tool
# retrieves the most text.
_FAILURE_ENVELOPE = "tool execution failed"


def _is_error_content(content: Any) -> bool:
    if isinstance(content, dict):
        # A truthy structured `error` field is the harness saying it failed.
        # `exit_code` is deliberately NOT consulted: grep exits 1 for "no
        # matches", which the harness itself labels "not an error".
        if content.get("error"):
            return True
        content = content.get("content") or ""
    if isinstance(content, str):
        return _FAILURE_ENVELOPE in content.lower()
    return False


def tool_error_names(result: RolloutResult) -> list[str]:
    """Names of tools whose results carried an error payload, in order."""
    names: list[str] = []
    for ev in result.events:
        if ev.type != "tool.result":
            continue
        if _is_error_content(ev.data.get("content")):
            name = ev.data.get("name") or "unknown"
            if name not in names:
                names.append(name)
    return names


def detect(result: RolloutResult, level: int) -> list[str]:
    """Return the deterministic failure flags for one rollout."""
    flags: list[str] = []

    # The harness records why it gave up on session.fail. Distinguish the
    # provider returning empty completions -- a reproducible model-level
    # failure -- from genuine infrastructure trouble, or the fix list files
    # it as noise and nobody looks at it.
    fail_reason = next(
        (ev.data.get("reason") for ev in result.events
         if ev.type == "session.fail"),
        None,
    )
    if fail_reason == "empty_llm_response":
        flags.append("empty_llm_response")
    elif result.error or result.terminal_status in {"error", "failed"}:
        flags.append("infra_error")

    if result.terminal_status == "timeout":
        flags.append("timeout")

    # Neither the iteration cap nor thinking-budget exhaustion surfaces as a
    # status: the harness completes the session normally and records the
    # cause only as the completion reason on session.complete.
    #
    # Thinking exhaustion additionally REWRITES the assistant content with an
    # explanatory string (loop.py), so the raw "empty content plus
    # finish_reason=length" signature no longer exists by the time the event
    # is persisted. The completion reason is the only reliable signal.
    for ev in result.events:
        if ev.type != "session.complete":
            continue
        reason = ev.data.get("reason")
        if reason == "budget_exhausted":
            flags.append("step_cap")
        elif reason == "thinking_budget_exhausted":
            flags.append("empty_response")
        break

    if tool_error_names(result):
        flags.append("tool_error")

    if result.answer is None:
        flags.append("no_final_answer")

    # Literally no tool calls on a question that needs research. Any tool
    # counts: an agent that curls the GitHub API through `terminal` has done
    # real retrieval. Picking a poor path is Stage-2's wrong_retrieval_path,
    # not a deterministic failure.
    if level >= 2:
        if not any(ev.type == "tool.call" for ev in result.events):
            flags.append("no_tool_use")

    return flags
