"""LLM provider abstraction -- routes to the correct API mode.

Supports:
- chat_completions: OpenAI SDK chat.completions (default, works with most providers)
- anthropic_messages: Native Anthropic Messages API (for Claude models)

Phase 1 routes everything through chat_completions.  The structure is ready
for adding Anthropic native mode as a minimal change.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class APIMode(str, Enum):
    """Supported API call modes."""

    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"


def detect_api_mode(
    model_id: str,
    base_url: str | None = None,
    provider: str | None = None,
) -> APIMode:
    """Auto-detect the best API mode for a model.

    Phase 1: always returns ``CHAT_COMPLETIONS``.
    Phase 2 will return ``ANTHROPIC_MESSAGES`` for direct Anthropic endpoints.
    """
    if provider == "anthropic":
        # Phase 2: return APIMode.ANTHROPIC_MESSAGES
        logger.debug(
            "Anthropic provider detected for model %s; "
            "using chat_completions (native Anthropic support pending)",
            model_id,
        )
        return APIMode.CHAT_COMPLETIONS

    model_lower = model_id.lower()
    if "claude" in model_lower and base_url and "anthropic" in base_url.lower():
        # Phase 2: return APIMode.ANTHROPIC_MESSAGES
        logger.debug(
            "Anthropic endpoint detected for model %s; "
            "using chat_completions (native Anthropic support pending)",
            model_id,
        )
        return APIMode.CHAT_COMPLETIONS

    return APIMode.CHAT_COMPLETIONS


# ---------------------------------------------------------------------------
# Message format converters (Phase 2 stubs)
# ---------------------------------------------------------------------------


def openai_to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format messages to Anthropic Messages API format.

    OpenAI:     ``{"role": "user", "content": "hello"}``
    Anthropic:  same but system is separate, tool results have different format.

    Phase 2 implementation will handle:
    - Extracting system messages (they go in a separate ``system`` param)
    - Converting tool_use / tool_result to Anthropic's content block format
    - Handling multi-part content (text + images)
    """
    # Phase 2 stub -- returns messages unmodified for now.
    converted: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system":
            # Phase 2: system messages extracted separately.
            continue
        converted.append(msg)
    return converted


def anthropic_to_openai_response(response: Any) -> dict:
    """Convert Anthropic response to OpenAI ChatCompletion format.

    Phase 2 implementation will map:
    - ``response.content`` blocks -> ``message.content`` + ``message.tool_calls``
    - ``response.usage`` -> ``usage`` with prompt_tokens / completion_tokens
    - ``response.stop_reason`` -> ``finish_reason``
    """
    # Phase 2 stub -- returns a minimal dict.
    return {
        "role": "assistant",
        "content": str(response),
    }


async def call_anthropic_messages(
    client: Any,  # anthropic.AsyncAnthropic (Phase 2)
    model: str,
    messages: list[dict],
    system: str,
    tools: list[dict] | None = None,
    *,
    stream: bool = False,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    extra_kwargs: dict | None = None,
) -> tuple[dict, dict]:
    """Call the Anthropic Messages API.

    Converts OpenAI-format messages to Anthropic format, calls the API,
    and converts the response back to OpenAI format so the rest of the
    harness doesn't need to know which API was used.

    Phase 2 implementation.  Currently raises ``NotImplementedError``.
    """
    raise NotImplementedError(
        "Native Anthropic Messages API support is not yet implemented. "
        "Use chat_completions mode (via OpenRouter or compatible proxy) "
        "for Claude models in Phase 1."
    )
