"""Command builders and ``stream-json`` output parsing for coding agents.

A focused port of the headless-cli invocation logic for the two agents we
support.  Both vendor CLIs run in non-interactive ``stream-json`` modes (no
PTY — sidesteps the missing-``ptyprocess`` sandbox gotcha).

The exact vendor flags and JSONL schemas here follow the design spec; they are
validated structurally by unit tests and must be re-confirmed against the real
``claude`` / ``codex`` binaries during the execution-layer isolation preflight
before the run path is enabled in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

_VALID_EFFORTS: Final[frozenset[str]] = frozenset(
    {"low", "medium", "high", "xhigh"}
)


@dataclass
class CodeInvocation:
    """A fully-resolved vendor-CLI invocation.

    ``argv`` is the raw argument vector (never shell-interpolated).  ``stdin``
    carries the prompt when the CLI reads it from standard input — keeping the
    (potentially large, quote-laden, sensitive) prompt out of the process
    argument list and any command log.
    """

    argv: list[str]
    stdin: str | None


@dataclass
class CodeResult:
    """The parsed outcome of a streamed run."""

    final_message: str
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    # Codex refreshes its own ``auth.json`` in-pod; when it changed, the
    # updated contents are surfaced here so the worker can re-store the
    # bundle and keep the vault copy fresh (spec §6.2/§11.4).
    updated_codex_auth_json: str | None = None


def build_invocation(
    agent: str,
    prompt: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    read_only: bool = False,
) -> CodeInvocation:
    """Build the vendor-CLI invocation for *agent*, or raise ``ValueError``."""
    if agent not in ("claude", "codex"):
        raise ValueError(f"Unknown agent {agent!r}; expected 'claude' or 'codex'.")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt is empty.")
    if effort is not None and effort not in _VALID_EFFORTS:
        raise ValueError(
            f"Unknown effort {effort!r}; expected one of "
            f"{', '.join(sorted(_VALID_EFFORTS))}."
        )

    if agent == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
        if read_only:
            argv += ["--permission-mode", "plan"]
        else:
            argv += ["--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        # Claude reads the prompt from stdin in print mode.
        return CodeInvocation(argv=argv, stdin=prompt)

    # codex
    argv = ["codex", "exec", "--json", "--skip-git-repo-check"]
    if read_only:
        argv += ["--sandbox", "read-only"]
    else:
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    # Codex takes the prompt as the final positional argument.
    argv.append(prompt)
    return CodeInvocation(argv=argv, stdin=None)


def _iter_json_lines(raw: str):
    """Yield parsed JSON objects from *raw*, tolerantly skipping bad lines."""
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _extract_text(value: object) -> str | None:
    """Pull a text string out of a message-content-ish value."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in (None, "text"):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "".join(parts)
    return None


def _usage_tokens(usage: object) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` from a usage-ish mapping."""
    if not isinstance(usage, dict):
        return 0, 0
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    try:
        return int(inp), int(out)
    except (TypeError, ValueError):
        return 0, 0


def parse_stream(agent: str, raw: str) -> CodeResult:
    """Mine a tolerant ``stream-json`` transcript for the final message + usage.

    Works across both vendors' shapes: it prefers an explicit terminal result
    event, otherwise falls back to the last assistant/agent text seen.  Any
    usage block encountered updates the token counts.
    """
    final_message = ""
    last_assistant = ""
    input_tokens = 0
    output_tokens = 0
    saw_any = False

    for obj in _iter_json_lines(raw):
        saw_any = True
        obj_type = obj.get("type")

        # Token usage may ride on any event (turn/result/usage-bearing line).
        if "usage" in obj:
            inp, out = _usage_tokens(obj.get("usage"))
            input_tokens = inp or input_tokens
            output_tokens = out or output_tokens

        # Claude: terminal "result" event carries the canonical final string.
        if obj_type == "result":
            result_text = obj.get("result")
            if isinstance(result_text, str):
                final_message = result_text
            inp, out = _usage_tokens(obj.get("usage"))
            input_tokens = inp or input_tokens
            output_tokens = out or output_tokens
            continue

        # Claude: assistant message events.
        if obj_type == "assistant":
            message = obj.get("message")
            if isinstance(message, dict):
                text = _extract_text(message.get("content"))
                if text:
                    last_assistant = text
            continue

        # Codex: agent_message items (wrapped in item.completed or bare).
        item = obj.get("item") if isinstance(obj.get("item"), dict) else obj
        if isinstance(item, dict) and item.get("type") in (
            "agent_message",
            "assistant_message",
        ):
            text = item.get("text") or _extract_text(item.get("content"))
            if isinstance(text, str) and text:
                last_assistant = text

    if not final_message:
        final_message = last_assistant

    error: str | None = None
    if not final_message:
        error = (
            "No final message found in the agent output stream."
            if saw_any
            else "The agent produced no parseable output."
        )

    return CodeResult(
        final_message=final_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error,
    )
