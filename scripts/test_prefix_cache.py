"""Live test for upstream prefix caching.

Sends two chat-completions requests to the configured LLM provider and
reports whether the second one hit the provider's implicit prefix cache.

Usage::

    uv run python scripts/test_prefix_cache.py [path/to/config.yaml]

Defaults to ``config.dev.yaml`` next to this script's parent directory.
Requires ``llm.base_url`` / ``llm.api_key`` / ``llm.model`` in the YAML.

The two requests share an identical leading prefix:

    [system, prefill_long_history..., user_A]

then request 2 extends with two more turns:

    + [assistant_A, user_B]

If the provider implements implicit prefix caching (DashScope Qwen,
OpenAI gpt-4o, Anthropic with cache_control, etc.) request 2's usage
should report a sizeable ``cached_tokens`` count.

The prefix has to clear the provider's minimum-cacheable-prefix
threshold (DashScope: 256 tokens; OpenAI: 1024).  We pad the system
prompt with deterministic filler to comfortably clear both.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml
from openai import AsyncOpenAI


def _load_llm_settings(config_path: Path) -> dict[str, str]:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    llm = data.get("llm") or {}
    for key in ("model", "base_url", "api_key"):
        if not llm.get(key):
            raise SystemExit(f"config {config_path} is missing llm.{key}")
    return {
        "model": str(llm["model"]),
        "base_url": str(llm["base_url"]),
        "api_key": str(llm["api_key"]),
    }


_FILLER_SENTENCE = (
    "This is deterministic filler content to push the shared prefix "
    "past the provider's minimum-cacheable-prefix threshold so the "
    "cache lookup is even attempted. "
)


def _padded_system_prompt(min_chars: int = 6000) -> str:
    """Stable system prompt that clears both 256-token and 1024-token mins."""
    repeats = (min_chars // len(_FILLER_SENTENCE)) + 1
    return (
        "You are a deterministic test assistant. Reply with one short sentence.\n\n"
        "# Stable filler (do not reference)\n"
        + _FILLER_SENTENCE * repeats
    )


def _shared_history(system_prompt: str) -> list[dict[str, str]]:
    """The byte-identical prefix used by both requests."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Round 1: name a color."},
        {
            "role": "assistant",
            "content": "Blue.",
        },
        {"role": "user", "content": "Round 2: name a fruit."},
        {
            "role": "assistant",
            "content": "Apple.",
        },
        {"role": "user", "content": "Round 3: name a country."},
    ]


def _extract_cached(usage: Any) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if isinstance(cached, (int, float)):
            return int(cached)
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
            return int(details["cached_tokens"])
    for attr in ("cache_read_input_tokens", "cached_tokens"):
        v = getattr(usage, attr, None)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _print_usage(label: str, response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    cached = _extract_cached(usage)
    msg = (
        getattr(response.choices[0].message, "content", "") or ""
    ).strip().replace("\n", " ")
    print(f"  {label}: prompt={prompt}  completion={completion}  cached={cached}")
    print(f"         reply: {msg[:80]}")
    return prompt, cached


async def main() -> int:
    config_path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent / "config.dev.yaml"
    )
    settings = _load_llm_settings(config_path)

    print(f"Provider: {settings['base_url']}")
    print(f"Model:    {settings['model']}")
    print(f"System prompt size: {len(_padded_system_prompt())} chars")
    print()

    client = AsyncOpenAI(api_key=settings["api_key"], base_url=settings["base_url"])

    system_prompt = _padded_system_prompt()
    request_1_messages = _shared_history(system_prompt)

    print("Request 1 (priming the cache):")
    resp_1 = await client.chat.completions.create(
        model=settings["model"],
        messages=request_1_messages,
        temperature=0,
        max_tokens=20,
    )
    prompt_1, cached_1 = _print_usage("usage", resp_1)
    print()

    # Request 2 extends request 1 by exactly two turns appended at the END.
    # The leading prefix is byte-identical.
    assistant_round_3 = (resp_1.choices[0].message.content or "X").strip()
    request_2_messages = request_1_messages + [
        {"role": "assistant", "content": assistant_round_3},
        {"role": "user", "content": "Round 4: name a planet."},
    ]

    print("Request 2 (shared prefix + 2 new tail turns):")
    resp_2 = await client.chat.completions.create(
        model=settings["model"],
        messages=request_2_messages,
        temperature=0,
        max_tokens=20,
    )
    prompt_2, cached_2 = _print_usage("usage", resp_2)
    print()

    print("Verdict:")
    if cached_2 > 0:
        ratio = (cached_2 / prompt_2 * 100) if prompt_2 else 0
        print(
            f"  PASS - provider returned cached_tokens={cached_2} "
            f"({ratio:.0f}% of prompt={prompt_2})."
        )
        print(
            "  The harness's shared-prefix request shape activates "
            "implicit prefix caching."
        )
    else:
        print(
            f"  NO HIT - cached_tokens=0 on request 2 (prompt={prompt_2})."
        )
        print(
            "  Either the provider doesn't return cached_tokens in usage, "
            "or the prefix is shorter than the cacheable minimum."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
