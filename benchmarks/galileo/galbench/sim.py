"""User and tool simulators plus the shared OpenAI-compatible transport.

Prompts are verbatim from upstream (v2/evaluate/config.py); only the
transport differs -- one OpenAI-compatible endpoint (the same deployment
the sibling benchmarks judge with) plays both roles at temperature 0,
where upstream used gpt-4.1 (user) and gpt-4.1-mini (tools). A scaffold
difference, recorded in RESULTS; within our own runs it is a constant.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

USER_SIMULATOR_PROMPT = """You are replying like a user with the following persona:
{persona_json}

You are participating in a scenario with these details:
{scenario_json}

CONVERSATION HISTORY:
{conversation_history}

TOOL OUTPUTS:
{tool_outputs}

Respond as this user based on their persona and scenario goals.

BEHAVIOR GUIDELINES:
1. Respond appropriately to the questions asked by the assistant.
2. Check if the assistant has completed all the tasks in the user_goals. If not then ask the assistant to complete the remaining tasks.
3. If assistant indicates a request is unsupported: don't repeat it, move to another goal.
4. Keep responses natural and realistic for your persona.
5. If you are not sure about the answer, say you do not know.
6. Respond in a concise manner. No need to thank the assistant for the help.
7. Do not discuss anything beyond what is needed to complete the goals.
8. If the assistant is not able to complete the goals, skip and move to remaining goals. Do not ask the assistant to repeat the same goal again."""

TOOL_SIMULATOR_PROMPT = """You are a tool simulator for evaluating AI agents. Generate a realistic response that STRICTLY conforms to the given RESPONSE SCHEMA and is contextually relevant to the ongoing conversation and the agent's action.

TOOL NAME: {tool_name}
TOOL PARAMETERS: {tool_parameters}
RESPONSE SCHEMA: {response_schema}

CONVERSATION HISTORY:
{conversation_history}

AGENT'S ACTION:
{agent_action}

STRICT REQUIREMENTS:
1. Your response MUST be a valid JSON object
2. ALL required fields specified in the schema MUST be present
3. Each field MUST match the exact type specified in the schema (string, number, boolean, etc.)
4. Enum fields MUST only use values from the specified enum list
5. Do not add fields that are not in the schema
6. Ensure all nested objects and arrays match their schema definitions

Generate a valid JSON response that exactly matches the schema and would realistically be returned by this tool."""


class SimError(Exception):
    """A simulator call failed or returned something unusable."""


ChatFn = Callable[[list[dict]], Awaitable[str]]


def make_chat(
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 180.0,
    max_tokens: int = 2000,
) -> ChatFn:
    """Plain-text chat completion against an OpenAI-compatible endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def chat(messages: list[dict]) -> str:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0)
        ) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_key}"}, json=body
            )
        if resp.status_code >= 400:
            raise SimError(
                f"simulator call failed (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )
        data = resp.json()
        choices = data.get("choices") or []
        content = (choices[0].get("message", {}).get("content") or "") if choices else ""
        if not content.strip():
            raise SimError("simulator returned empty content")
        return content

    return chat


def format_history(transcript: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{t['role'].upper()}: {t['content']}" for t in transcript
    ) or "(conversation just started)"


async def simulate_user(
    chat: ChatFn,
    persona: dict[str, Any],
    user_goals: tuple[str, ...],
    transcript: list[dict[str, str]],
    tool_outputs: list[dict[str, Any]],
) -> str:
    prompt = USER_SIMULATOR_PROMPT.format(
        persona_json=json.dumps(persona, indent=2, default=str),
        scenario_json=json.dumps({"user_goals": list(user_goals)}, indent=2),
        conversation_history=format_history(transcript),
        tool_outputs=json.dumps(tool_outputs[-10:], indent=2, default=str),
    )
    return (await chat([{"role": "user", "content": prompt}])).strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    candidates = [text.strip()]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def simulate_tool(
    chat: ChatFn,
    tool: dict[str, Any],
    tool_args: dict[str, Any],
    transcript: list[dict[str, str]],
    agent_action: str,
) -> dict[str, Any]:
    """One simulated tool response, schema-conforming best-effort.

    An unusable simulator reply degrades to an explicit error object the
    agent (and later the judge) can see -- never a crashed scenario.
    """
    prompt = TOOL_SIMULATOR_PROMPT.format(
        tool_name=tool["title"],
        tool_parameters=json.dumps(tool_args, ensure_ascii=False, default=str),
        response_schema=json.dumps(tool.get("response_schema") or {},
                                   ensure_ascii=False),
        conversation_history=format_history(transcript),
        agent_action=agent_action[:2000],
    )
    try:
        reply = await chat([{"role": "user", "content": prompt}])
    except SimError as exc:
        return {"error": f"tool simulator unavailable: {exc}"}
    parsed = _extract_json_object(reply)
    if parsed is None:
        return {"error": "tool simulator returned non-JSON output"}
    return parsed
