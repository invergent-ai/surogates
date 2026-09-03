"""Rubric judging: one OpenAI-compatible completion per task.

The verdict contract is upstream's (see evaluation/src/agent_as_a_judge.py
in OpenDataBox/Workspace-Bench): a strict evaluator, one JSON object of
per-rubric ``{index, passed, confidence, evidence}`` rows, and the rule
that insufficient evidence means failed. What differs -- documented, and
the reason our number is not leaderboard-comparable -- is delivery:
upstream's judge is itself an agent inspecting the task container, ours
receives the collected output files inline (extracted to text) plus a
compact action trace for the process rubrics.

Transport is lifted from benchmarks/gaia: ``response_format: json_schema``
requested, fenced/prose-wrapped JSON tolerated, empty thinking-burn
replies surfaced as errors rather than parse failures.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from wsbench.client import Event
from wsbench.dataset import Task

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Judge payload budgets. A task has at most ~25 rubrics and a handful of
# output files; these caps keep the single completion well inside any
# sane context window while leaving room for real documents.
MAX_TOTAL_FILE_CHARS = 150_000
MAX_TRAJECTORY_CHARS = 20_000


class JudgeError(Exception):
    """The judge call failed or returned something unusable."""


@dataclass
class RubricVerdict:
    index: int
    rubric: str
    rubric_type: str
    passed: bool
    confidence: float
    evidence: str


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply, tolerating fences/prose."""
    candidates: list[str] = [text.strip()]

    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise JudgeError(f"could not parse a JSON object from reply: {text[:200]!r}")


CompleteFn = Callable[[list[dict], dict], Awaitable[dict]]


def make_openai_complete(
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 300.0,
    # Reasoning models can burn thousands of hidden tokens before the
    # JSON: an 8k cap produced an empty reply (finish_reason=length) on
    # a 31-rubric task, so leave generous headroom.
    max_tokens: int = 20000,
) -> CompleteFn:
    """Build the ``complete(messages, schema)`` callable ``judge_task`` expects."""
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def complete(messages: list[dict], schema: dict) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0)
        ) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"}, json=body
            )
        if resp.status_code >= 400:
            raise JudgeError(
                f"judge call failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        choices = data.get("choices") or []
        content = (choices[0].get("message", {}).get("content") or "") if choices else ""
        if not content.strip():
            finish = choices[0].get("finish_reason") if choices else None
            raise JudgeError(
                f"judge returned empty content (finish_reason={finish!r})"
            )
        return _extract_json(content)

    return complete


RUBRICS_SCHEMA = {
    "name": "rubric_verdicts",
    "schema": {
        "type": "object",
        "properties": {
            "rubrics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "passed": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["index", "passed", "evidence"],
                },
            }
        },
        "required": ["rubrics"],
    },
}

_SYSTEM = "You are a strict task evaluator."

# Adapted from upstream's English judge instructions, minus the
# filesystem-inspection lines that do not apply to an inline judge.
_INSTRUCTIONS = (
    "Evaluate each numbered rubric against the evidence below: the "
    "candidate's output files (extracted to text) and the execution "
    "trace.\n"
    "Base judgments only on content actually present in the evidence; "
    "do not assume facts.\n"
    "A rubric about a file that was not produced, or whose content is "
    "marked not extractable, fails -- say what evidence is missing.\n"
    "Rubrics about the working process (which files were consulted, how "
    "the work was carried out) are judged against the execution trace.\n"
    "If evidence is insufficient: passed=false and explain what evidence "
    "is missing.\n"
    "Output only one JSON object in this exact shape: "
    '{ "rubrics": [ {"index":0,"passed":true,"confidence":0.8,'
    '"evidence":"..."}, ... ] }\n'
    "Return a row for every rubric index, starting at 0."
)


def format_trajectory(events: list[Event], max_chars: int = MAX_TRAJECTORY_CHARS) -> str:
    """Render a trace as readable text, dropping per-token delta noise."""
    lines: list[str] = []
    for ev in events:
        if ev.type == "tool.call":
            args = json.dumps(ev.data.get("arguments") or {}, default=str)
            lines.append(f"[{ev.id}] CALL {ev.data.get('name')} {args[:500]}")
        elif ev.type == "tool.result":
            content = ev.data.get("content")
            text = content if isinstance(content, str) else json.dumps(
                content, default=str
            )
            lines.append(f"[{ev.id}] RESULT {ev.data.get('name')} {text[:500]}")
        elif ev.type == "llm.response":
            text = (ev.data.get("message") or {}).get("content") or ""
            lines.append(f"[{ev.id}] ASSISTANT {text[:1000]}")
        elif ev.type == "user.message":
            lines.append(f"[{ev.id}] USER {(ev.data.get('content') or '')[:500]}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        # Head and tail carry more signal than the middle of a tool loop.
        half = max_chars // 2
        out = out[:half] + "\n...[trajectory truncated]...\n" + out[-half:]
    return out


def build_judge_prompt(
    task: Task,
    files: list[dict[str, Any]],
    trajectory: str,
    final_message: str,
) -> str:
    """Assemble the single user message for one task's rubric evaluation.

    ``files`` rows: {workspace_path, text, note} -- already extracted.
    Total inlined file content is capped; files beyond the cap are listed
    with a note instead of content so the judge still sees they exist.
    """
    rubric_lines = [
        f"{i}. [{rtype}] {rubric}"
        for i, (rubric, rtype) in enumerate(
            zip(task.rubrics, _padded_types(task))
        )
    ]

    file_blocks: list[str] = []
    budget = MAX_TOTAL_FILE_CHARS
    for row in files:
        header = f"=== FILE: {row['workspace_path']}"
        if row.get("note"):
            header += f" ({row['note']})"
        header += " ==="
        text = row.get("text") or ""
        if budget <= 0 and text:
            file_blocks.append(header + "\n[content omitted: total budget reached]")
            continue
        text = text[:budget]
        budget -= len(text)
        file_blocks.append(header + ("\n" + text if text else ""))

    if not file_blocks:
        file_blocks.append("(the agent produced no files)")

    return (
        f"{_INSTRUCTIONS}\n\n"
        f"TASK (persona: {task.persona}):\n{task.instruction.strip()}\n\n"
        f"EXPECTED OUTPUT FILES: {json.dumps(list(task.output_files))}\n\n"
        f"RUBRICS:\n" + "\n".join(rubric_lines) + "\n\n"
        f"CANDIDATE OUTPUT FILES:\n" + "\n\n".join(file_blocks) + "\n\n"
        f"FINAL AGENT MESSAGE:\n{final_message[:4000] or '(none)'}\n\n"
        f"EXECUTION TRACE:\n{trajectory or '(no trace)'}"
    )


def _padded_types(task: Task) -> list[str]:
    """rubric_types aligned to rubrics; pad when upstream rows disagree."""
    types = list(task.rubric_types)
    while len(types) < len(task.rubrics):
        types.append("Unspecified")
    return types[: len(task.rubrics)]


async def judge_task(
    complete: CompleteFn,
    task: Task,
    files: list[dict[str, Any]],
    events: list[Event],
    final_message: str,
) -> list[RubricVerdict]:
    """Grade one task. Always returns one verdict per rubric.

    A rubric the judge did not answer is a *failed* rubric with explicit
    evidence saying so -- silently dropping it would inflate the score.
    """
    prompt = build_judge_prompt(
        task, files, format_trajectory(events), final_message
    )
    reply = await complete(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        RUBRICS_SCHEMA,
    )

    rows = reply.get("rubrics")
    if not isinstance(rows, list):
        raise JudgeError(f"judge reply has no rubrics list: {str(reply)[:200]}")

    types = _padded_types(task)
    by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("index"), int):
            by_index.setdefault(row["index"], row)

    verdicts: list[RubricVerdict] = []
    for i, rubric in enumerate(task.rubrics):
        row = by_index.get(i)
        if row is None:
            verdicts.append(RubricVerdict(
                index=i, rubric=rubric, rubric_type=types[i],
                passed=False, confidence=0.0,
                evidence="judge returned no verdict for this rubric",
            ))
            continue
        confidence = row.get("confidence")
        verdicts.append(RubricVerdict(
            index=i, rubric=rubric, rubric_type=types[i],
            passed=bool(row.get("passed") is True),
            confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
            evidence=str(row.get("evidence") or ""),
        ))
    return verdicts
