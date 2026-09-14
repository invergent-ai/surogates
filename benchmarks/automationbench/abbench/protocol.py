"""The text tool-call protocol between the orchestrator and the agent.

Upstream binds the api toolset natively into its own loop; a prod
harness session cannot be handed those callables, so the orchestrator
speaks the same fenced ``tool_call`` protocol the galileo benchmark
proved out: the first message carries the task's own system prompt
(verbatim), the tool catalog, and the calling format; the agent emits
tool_call blocks and receives TOOL RESULTS messages back, ending with
TASK_COMPLETE.
"""
from __future__ import annotations

import json
import re
from typing import Any

_TOOL_CALL_RE = re.compile(
    r"```tool_call\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
COMPLETE_MARKER = "TASK_COMPLETE"

PREAMBLE_TEMPLATE = """{system_prompt}

TOOLS AVAILABLE TO YOU (call them, do not describe them):
{catalog}

HOW TO CALL A TOOL: emit one fenced block per call, exactly like this,
then stop and wait for the results before continuing:

```tool_call
{{"tool_name": "<name>", "tool_args": {{"<param>": <value>}}}}
```

You may emit several tool_call blocks in one reply; they run in order.
Never invent tool results -- wait for the TOOL RESULTS message.
Discover endpoints with api_search before calling them. When every part
of the task is done, end your reply with {marker}.

TASK:
{user_prompt}"""


def build_preamble(system_prompt: str, user_prompt: str,
                   catalog: list[dict[str, Any]]) -> str:
    return PREAMBLE_TEMPLATE.format(
        system_prompt=system_prompt.strip(),
        catalog=json.dumps(catalog, ensure_ascii=False, indent=1),
        marker=COMPLETE_MARKER,
        user_prompt=user_prompt.strip(),
    )


def parse_tool_calls(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract tool_call blocks; malformed blocks come back as errors."""
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for block in _TOOL_CALL_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON in tool_call block: {exc}")
            continue
        if not isinstance(data, dict) or not data.get("tool_name"):
            errors.append("tool_call block missing tool_name")
            continue
        args = data.get("tool_args")
        calls.append({
            "tool_name": str(data["tool_name"]),
            "tool_args": args if isinstance(args, dict) else {},
        })
    return calls, errors


def is_complete(text: str) -> bool:
    return COMPLETE_MARKER in (text or "")


def tool_results_message(results: list[dict[str, Any]]) -> str:
    lines = ["TOOL RESULTS:"]
    for r in results:
        lines.append(json.dumps(r, ensure_ascii=False, default=str))
    lines.append(
        "\nContinue the task based on these results. Remember to end "
        f"with {COMPLETE_MARKER} once everything is done."
    )
    return "\n".join(lines)
