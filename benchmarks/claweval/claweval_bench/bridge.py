"""Map surogates session events onto claw-eval trace models for grading.

Graders take ``messages`` (the conversation), ``dispatches`` (tool traffic)
and ``audit_data`` (service logs). The conversation is rebuilt from session
events; dispatches come from the MCP adapter's log, which is authoritative
for endpoint/status/latency -- the session's own tool events describe the
harness's view of the call, not the wire traffic the graders reason about.

Deliberate approximations, acceptable because grading here is diagnostic
rather than leaderboard-comparable:

* tool_use ids are the harness's call ids and do not match the adapter's
  synthesized dispatch ids; no vendored grader joins on them.
* Non-text session artifacts (files, board updates) are not represented.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from claweval_bench.client import Event


def to_messages(events: list[Event], question: str) -> list[Any]:
    """Rebuild the conversation as claw-eval TraceMessages."""
    from claw_eval.models.content import TextBlock, ToolResultBlock, ToolUseBlock
    from claw_eval.models.message import Message
    from claw_eval.models.trace import TokenUsage, TraceMessage

    messages: list[Any] = [
        TraceMessage(trace_id="", message=Message(role="user", content=question)),
    ]
    for ev in events:
        if ev.type == "llm.response":
            msg = ev.data.get("message") or {}
            blocks: list[Any] = []
            content = msg.get("content") or ""
            if content:
                blocks.append(TextBlock(text=content))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                blocks.append(ToolUseBlock(
                    id=str(tc.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    input=args if isinstance(args, dict) else {},
                ))
            if not blocks:
                continue
            messages.append(TraceMessage(
                trace_id="",
                message=Message(role="assistant", content=blocks),
                usage=TokenUsage(
                    input_tokens=int(ev.data.get("input_tokens") or 0),
                    output_tokens=int(ev.data.get("output_tokens") or 0),
                ),
            ))
        elif ev.type == "tool.result":
            text = ev.data.get("content")
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            messages.append(TraceMessage(
                trace_id="",
                message=Message(role="user", content=[ToolResultBlock(
                    tool_use_id=str(ev.data.get("tool_call_id") or ""),
                    content=[TextBlock(text=text[:20000])],
                    is_error=False,
                )]),
            ))
    return messages


def load_dispatches(path: pathlib.Path) -> list[Any]:
    """Read the adapter's JSONL log as claw-eval ToolDispatch events."""
    from claw_eval.models.trace import ToolDispatch

    if not path.exists():
        return []
    dispatches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        dispatches.append(ToolDispatch(trace_id="", **record))
    return dispatches
