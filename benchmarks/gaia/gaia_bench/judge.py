"""OpenAI-compatible completion used by Stage-2 failure attribution.

Kept separate from ``attribute`` so the classification prompt and the
transport can be tested independently -- ``attribute`` takes an injected
``complete`` callable and never touches the network in tests.

Structured output is requested via ``response_format: json_schema`` rather
than the project's usual ``generate_structured``, which lives in
``surogates.harness`` and cannot be imported here (see the package's
no-product-imports rule).
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class JudgeError(Exception):
    """The classifier call failed or returned something unusable."""


def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply.

    Not every OpenAI-compatible proxy honours ``response_format``; some
    wrap the object in a markdown fence or surround it with prose. Failing
    the whole analysis over that would be brittle, so we degrade in steps
    and only give up when there is genuinely no object present.
    """
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
    timeout: float = 180.0,
    max_tokens: int = 1500,
) -> CompleteFn:
    """Build the ``complete(messages, schema)`` callable ``attribute`` expects."""
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
                f"classifier call failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        choices = data.get("choices") or []
        content = (choices[0].get("message", {}).get("content") or "") if choices else ""
        if not content.strip():
            # Empty content with finish_reason=length is the thinking-burn
            # signature: every token spent on hidden reasoning, nothing
            # visible returned. Surface it rather than treating it as a
            # parse failure, so the cause is obvious in the logs.
            finish = choices[0].get("finish_reason") if choices else None
            raise JudgeError(
                f"classifier returned empty content (finish_reason={finish!r})"
            )
        return _extract_json(content)

    return complete
