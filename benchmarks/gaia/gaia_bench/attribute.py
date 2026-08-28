"""LLM attribution for failures the deterministic detectors cannot explain.

Runs only where every tool worked and the answer is still wrong. Each
verdict must cite trajectory evidence, which is what stops
``ambiguous_ground_truth`` from quietly absorbing real failures.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from gaia_bench.client import Event

# Root cause -> the harness component that owns the fix.
ROOT_CAUSES: dict[str, str] = {
    "search_no_results": "web_search",
    "search_wrong_source": "web_search",
    "wrong_retrieval_path": "tool_selection",
    "page_fetch_failed": "browser",
    "page_content_missed": "browser",
    "file_parse_failed": "file_ops",
    "file_content_missed": "file_ops",
    "image_misread": "vision",
    "audio_video_unsupported": "media",
    "computation_error": "terminal",
    "reasoning_error": "model_prompt",
    "gave_up_early": "iteration_caps",
    "ambiguous_ground_truth": "benchmark",
}

ATTRIBUTION_SCHEMA: dict[str, Any] = {
    "name": "gaia_failure_attribution",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["root_cause", "evidence", "hypothesis"],
        "properties": {
            "root_cause": {
                "type": "string",
                "enum": sorted(ROOT_CAUSES),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "Quote or cite the specific trajectory step that "
                    "justifies this root cause."
                ),
            },
            "hypothesis": {
                "type": "string",
                "description": "What change to the harness would fix this.",
            },
        },
    },
}

_SKIP_TYPES = {"llm.delta"}

def _system_prompt() -> str:
    """Build the classifier prompt.

    The allowed labels and the JSON-only requirement are stated here rather
    than left to ``response_format``: some OpenAI-compatible proxies (yunwu
    among them) return 200 OK and ignore the json_schema entirely, replying
    in markdown prose with invented labels. Pinning both in the prompt is
    what makes the verdicts usable on those providers.
    """
    causes = "\n".join(
        f"- {cause} (owner: {owner})" for cause, owner in sorted(ROOT_CAUSES.items())
    )
    return (
        "You diagnose why an AI agent got a benchmark question wrong. "
        "The agent's tools all executed without error, so the failure is in "
        "retrieval, extraction, interpretation, or reasoning.\n\n"
        "Choose exactly ONE root cause from this closed list. Do not invent "
        "new labels:\n"
        f"{causes}\n\n"
        "Use ambiguous_ground_truth ONLY when the trajectory shows the source "
        "genuinely no longer contains the expected answer.\n\n"
        "Reply with a bare JSON object and NOTHING else -- no markdown, no "
        "code fence, no commentary. Exactly these keys:\n"
        '{"root_cause": "<one label from the list above>", '
        '"evidence": "<the specific trajectory step justifying it>", '
        '"hypothesis": "<what harness change would fix it>"}'
    )


_SYSTEM = _system_prompt()


def format_trajectory(events: list[Event], max_chars: int = 40000) -> str:
    """Render a trace as readable text, dropping per-token delta noise."""
    lines: list[str] = []
    for ev in events:
        if ev.type in _SKIP_TYPES:
            continue
        if ev.type == "tool.call":
            args = json.dumps(ev.data.get("arguments") or {}, default=str)
            lines.append(f"[{ev.id}] CALL {ev.data.get('name')} {args}")
        elif ev.type == "tool.result":
            content = ev.data.get("content")
            text = content if isinstance(content, str) else json.dumps(
                content, default=str
            )
            lines.append(f"[{ev.id}] RESULT {ev.data.get('name')} {text}")
        elif ev.type == "llm.response":
            text = (ev.data.get("message") or {}).get("content") or ""
            lines.append(f"[{ev.id}] ASSISTANT {text}")
        elif ev.type == "user.message":
            lines.append(f"[{ev.id}] USER {ev.data.get('content') or ''}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        # Keep the head and tail: the setup and the conclusion carry more
        # diagnostic signal than the middle of a long tool loop.
        half = max_chars // 2
        out = out[:half] + "\n...[trajectory truncated]...\n" + out[-half:]
    return out


CompleteFn = Callable[[list[dict], dict], Awaitable[dict]]


async def attribute(
    complete: CompleteFn,
    question: str,
    ground_truth: str,
    model_answer: str | None,
    events: list[Event],
) -> dict[str, Any]:
    """Classify one unexplained failure. Returns root_cause, owner, evidence."""
    user = (
        f"QUESTION:\n{question}\n\n"
        f"CORRECT ANSWER:\n{ground_truth}\n\n"
        f"AGENT ANSWER:\n{model_answer if model_answer is not None else '(none)'}\n\n"
        f"TRAJECTORY:\n{format_trajectory(events)}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    verdict = await complete(messages, ATTRIBUTION_SCHEMA)

    cause = verdict.get("root_cause")
    if cause not in ROOT_CAUSES:
        raise ValueError(f"attribution returned unknown root_cause: {cause!r}")

    return {
        "root_cause": cause,
        "owner": ROOT_CAUSES[cause],
        "evidence": verdict.get("evidence", ""),
        "hypothesis": verdict.get("hypothesis", ""),
    }
