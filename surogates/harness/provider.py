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
