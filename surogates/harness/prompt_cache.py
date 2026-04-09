"""Anthropic prompt caching -- injects cache_control markers for prefix cache hits.

When using Claude models (via OpenRouter or direct Anthropic), marking the system
prompt and early messages with cache_control causes Anthropic to cache the prefix.
Subsequent turns reuse the cached prefix, reducing input costs by ~75%.

Also provides a per-session system prompt cache so that the prompt is built once
per session and reused across turns (wake() calls), preserving Anthropic prefix
cache hits.
"""

from __future__ import annotations

import copy
import logging
from uuid import UUID

logger = logging.getLogger(__name__)

# Number of trailing user/assistant messages to mark with cache_control.
_CACHE_BREAKPOINT_COUNT: int = 3


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------


def is_cacheable_model(model_id: str, base_url: str | None = None) -> bool:
    """Check if the model supports Anthropic prompt caching."""
    model_lower = model_id.lower()
    if "claude" in model_lower:
        return True
    if base_url and "anthropic" in base_url.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# Cache control injection
# ---------------------------------------------------------------------------

_CACHE_MARKER = {"type": "ephemeral"}


def apply_cache_control(messages: list[dict], system_prompt: str) -> tuple[list[dict], str]:
    """Inject cache_control markers for Anthropic prompt caching.

    Strategy:
    - Mark the system prompt with cache_control (via metadata)
    - Mark the last ``_CACHE_BREAKPOINT_COUNT`` user/assistant messages
      with cache_control
    - This creates a stable prefix that Anthropic caches

    Returns modified (messages, system_prompt) -- both are *copies* of the
    originals so the caller's data is not mutated.
    """
    # Deep-copy to avoid mutating the caller's data.
    messages = copy.deepcopy(messages)

    # Find the last N user/assistant messages and inject cache markers.
    eligible_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        role = messages[i].get("role")
        if role in ("user", "assistant"):
            eligible_indices.append(i)
            if len(eligible_indices) >= _CACHE_BREAKPOINT_COUNT:
                break

    for idx in eligible_indices:
        messages[idx]["cache_control"] = _CACHE_MARKER

    return messages, system_prompt


# ---------------------------------------------------------------------------
# Extra body for OpenRouter / Anthropic API caching
# ---------------------------------------------------------------------------


def build_cache_extra_body(model_id: str) -> dict | None:
    """Build extra_body for Anthropic prompt caching via OpenRouter.

    Returns dict to pass as ``extra_body`` to ``chat.completions.create()``,
    or ``None`` if caching not applicable.
    """
    if not is_cacheable_model(model_id):
        return None

    return {
        "extra_headers": {
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
    }


# ---------------------------------------------------------------------------
# System prompt cache (per-session, stored on the worker)
# ---------------------------------------------------------------------------


class SystemPromptCache:
    """Caches the system prompt per session to preserve Anthropic prefix cache hits.

    The prompt is built on the first wake() and stored.  Subsequent wake()
    calls reuse the cached prompt.  Cache is invalidated only on context
    compression (which changes the conversation shape).
    """

    def __init__(self) -> None:
        self._cache: dict[UUID, str] = {}  # session_id -> system_prompt

    def get(self, session_id: UUID) -> str | None:
        """Return cached system prompt for *session_id*, or ``None``."""
        return self._cache.get(session_id)

    def set(self, session_id: UUID, prompt: str) -> None:
        """Store a system prompt for *session_id*."""
        self._cache[session_id] = prompt

    def invalidate(self, session_id: UUID) -> None:
        """Remove cached prompt for *session_id* (e.g. after compression)."""
        self._cache.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, session_id: UUID) -> bool:
        return session_id in self._cache
