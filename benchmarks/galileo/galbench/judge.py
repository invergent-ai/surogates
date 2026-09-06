"""AC and TSQ judging: two structured completions per scenario.

Upstream computes Action Completion and Tool Selection Quality with
Galileo's platform metrics; those are closed, so this reimplements their
published definitions with the same judge transport the sibling
benchmarks use (``response_format: json_schema`` requested, fenced JSON
tolerated, empty replies surfaced). AC: was each user goal actually
accomplished in the conversation, per the transcript and tool evidence.
TSQ: was each tool call the right tool with the right arguments at the
right point. Unanswered items are failed with explicit evidence, never
dropped.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

MAX_TRANSCRIPT_CHARS = 60_000


class JudgeError(Exception):
    """The judge call failed or returned something unusable."""


def _extract_json(text: str) -> dict[str, Any]:
    candidates: list[str] = [text.strip()]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
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
    max_tokens: int = 8000,
) -> CompleteFn:
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


AC_SCHEMA = {
    "name": "action_completion",
    "schema": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "accomplished": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["index", "accomplished", "evidence"],
                },
            }
        },
        "required": ["goals"],
    },
}

TSQ_SCHEMA = {
    "name": "tool_selection_quality",
    "schema": {
        "type": "object",
        "properties": {
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "good": {"type": "boolean"},
                        "issue": {"type": "string"},
                    },
                    "required": ["index", "good", "issue"],
                },
            }
        },
        "required": ["calls"],
    },
}

_SYSTEM = "You are a strict evaluator of AI assistant conversations."

_AC_INSTRUCTIONS = (
    "For each numbered user goal, decide whether the assistant actually "
    "accomplished it in this conversation. Accomplished means the "
    "concrete action was performed via an appropriate tool call whose "
    "result confirms it (or the requested information was retrieved and "
    "delivered accurately). Explaining how to do something, promising to "
    "do it, or claiming success without tool evidence does NOT count. A "
    "goal the user abandoned after the assistant correctly declared it "
    "unsupported counts as accomplished only if it was genuinely outside "
    "the tool catalog. Base judgments only on the transcript; if evidence "
    "is insufficient, accomplished=false and say what is missing. Return "
    "a row for every goal index, starting at 0."
)

_TSQ_INSTRUCTIONS = (
    "For each numbered tool call, decide whether it was a good call: the "
    "right tool for the immediate need, with correct and complete "
    "arguments consistent with the schema and the conversation (no "
    "invented values for information the user never gave), at a sensible "
    "point in the flow (no redundant repeats of already-obtained "
    "results, no calls for unsupported requests). Judge the call as "
    "made, regardless of what the simulated tool returned. Return a row "
    "for every call index, starting at 0."
)


@dataclass
class GoalVerdict:
    index: int
    goal: str
    accomplished: bool
    evidence: str


@dataclass
class CallVerdict:
    index: int
    tool_name: str
    good: bool
    issue: str


def _clip_transcript(transcript: list[dict[str, str]]) -> str:
    text = "\n\n".join(f"[{t['role'].upper()}]\n{t['content']}" for t in transcript)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        half = MAX_TRANSCRIPT_CHARS // 2
        text = text[:half] + "\n...[transcript truncated]...\n" + text[-half:]
    return text


async def judge_action_completion(
    complete: CompleteFn,
    user_goals: tuple[str, ...],
    transcript: list[dict[str, str]],
) -> list[GoalVerdict]:
    goals_block = "\n".join(f"{i}. {g}" for i, g in enumerate(user_goals))
    prompt = (
        f"{_AC_INSTRUCTIONS}\n\nUSER GOALS:\n{goals_block}\n\n"
        f"CONVERSATION:\n{_clip_transcript(transcript)}\n\n"
        'Output only one JSON object: {"goals": [{"index": 0, '
        '"accomplished": true, "evidence": "..."}, ...]}'
    )
    reply = await complete(
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": prompt}],
        AC_SCHEMA,
    )
    rows = reply.get("goals")
    if not isinstance(rows, list):
        raise JudgeError(f"AC reply has no goals list: {str(reply)[:200]}")
    by_index = {r["index"]: r for r in rows
                if isinstance(r, dict) and isinstance(r.get("index"), int)}
    verdicts = []
    for i, goal in enumerate(user_goals):
        row = by_index.get(i)
        if row is None:
            verdicts.append(GoalVerdict(i, goal, False,
                                        "judge returned no verdict for this goal"))
        else:
            verdicts.append(GoalVerdict(
                i, goal, bool(row.get("accomplished") is True),
                str(row.get("evidence") or ""),
            ))
    return verdicts


async def judge_tool_selection(
    complete: CompleteFn,
    tool_calls: list[dict[str, Any]],
    tool_catalog: str,
    transcript: list[dict[str, str]],
) -> list[CallVerdict]:
    if not tool_calls:
        return []
    calls_block = "\n".join(
        f"{i}. {c['tool_name']}({json.dumps(c['tool_args'], default=str)})"
        for i, c in enumerate(tool_calls)
    )
    prompt = (
        f"{_TSQ_INSTRUCTIONS}\n\nTOOL CATALOG:\n{tool_catalog}\n\n"
        f"TOOL CALLS MADE:\n{calls_block}\n\n"
        f"CONVERSATION:\n{_clip_transcript(transcript)}\n\n"
        'Output only one JSON object: {"calls": [{"index": 0, '
        '"good": true, "issue": ""}, ...]}'
    )
    reply = await complete(
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": prompt}],
        TSQ_SCHEMA,
    )
    rows = reply.get("calls")
    if not isinstance(rows, list):
        raise JudgeError(f"TSQ reply has no calls list: {str(reply)[:200]}")
    by_index = {r["index"]: r for r in rows
                if isinstance(r, dict) and isinstance(r.get("index"), int)}
    verdicts = []
    for i, call in enumerate(tool_calls):
        row = by_index.get(i)
        if row is None:
            verdicts.append(CallVerdict(i, call["tool_name"], False,
                                        "judge returned no verdict for this call"))
        else:
            verdicts.append(CallVerdict(
                i, call["tool_name"], bool(row.get("good") is True),
                str(row.get("issue") or ""),
            ))
    return verdicts
