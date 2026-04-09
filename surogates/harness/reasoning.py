"""Thinking / reasoning extraction from LLM responses.

Extracts reasoning blocks from multiple provider-specific formats
(DeepSeek, Qwen, Moonshot, Novita, OpenRouter) and inline
``<think>``/``<thinking>`` XML tags.  Provides stripping of those
inline blocks after extraction so they are not duplicated in stored
events.

Additional edge-case handlers:

- **Thinking prefill continuation** — detects when the model produced
  reasoning but no visible content, allowing the caller to retry with
  the thinking visible.
- **Content-with-tools fallback** — caches text content from turns that
  also had tool calls, so a subsequent thinking-only turn can fall back
  to the cached content instead of retrying.
- **Incomplete scratchpad detection** — detects ``<REASONING_SCRATCHPAD>``
  tags that were opened but never closed (model ran out of tokens).
"""

from __future__ import annotations

import re
from typing import Any

THINK_RE: re.Pattern[str] = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>",
    re.DOTALL,
)

SCRATCHPAD_OPEN_RE: re.Pattern[str] = re.compile(
    r"<REASONING_SCRATCHPAD>",
    re.IGNORECASE,
)

SCRATCHPAD_CLOSE_RE: re.Pattern[str] = re.compile(
    r"</REASONING_SCRATCHPAD>",
    re.IGNORECASE,
)


def extract_reasoning(message: dict[str, Any]) -> str | None:
    """Extract reasoning text from an LLM response message.

    Checks multiple sources in priority order:
    1. ``message.reasoning`` (DeepSeek, Qwen provider extension)
    2. ``message.reasoning_content`` (Moonshot, Novita provider extension)
    3. Inline ``<think>...</think>`` or ``<thinking>...</thinking>`` blocks
    4. ``reasoning_details`` array (OpenRouter unified format)

    Returns the extracted reasoning text, or ``None`` if none was found.
    """
    # 1. Provider-specific field: reasoning
    reasoning = message.get("reasoning")
    if reasoning and isinstance(reasoning, str):
        return reasoning

    # 2. Provider-specific field: reasoning_content
    reasoning_content = message.get("reasoning_content")
    if reasoning_content and isinstance(reasoning_content, str):
        return reasoning_content

    # 3. Inline <think> or <thinking> blocks in content.
    content = message.get("content")
    if content and isinstance(content, str):
        matches = THINK_RE.findall(content)
        if matches:
            return "\n\n".join(m.strip() for m in matches if m.strip())

    # 4. reasoning_details array (OpenRouter).
    reasoning_details = message.get("reasoning_details")
    if reasoning_details and isinstance(reasoning_details, list):
        parts: list[str] = []
        for detail in reasoning_details:
            if isinstance(detail, dict):
                text = detail.get("content") or detail.get("text") or ""
                if text:
                    parts.append(str(text))
            elif isinstance(detail, str):
                parts.append(detail)
        if parts:
            return "\n\n".join(parts)

    return None


def strip_think_blocks(message: dict[str, Any]) -> None:
    """Remove ``<think>`` / ``<thinking>`` blocks from message content in-place.

    This is called *after* reasoning has been extracted so that the stored
    ``LLM_RESPONSE`` event does not contain duplicate reasoning text.
    """
    content = message.get("content")
    if content and isinstance(content, str):
        stripped = THINK_RE.sub("", content).strip()
        message["content"] = stripped if stripped else None


# ---------------------------------------------------------------------------
# Thinking prefill continuation
# ---------------------------------------------------------------------------


def is_thinking_only_response(message: dict[str, Any]) -> bool:
    """Return ``True`` if the message has reasoning but no visible content.

    This happens when the model produces a thinking block but the actual
    content is empty or ``None``.  The caller should retry with the
    thinking text visible so the model can complete the response.
    """
    content = (message.get("content") or "").strip()
    # Strip think blocks to see if anything remains.
    if content:
        visible = THINK_RE.sub("", content).strip()
    else:
        visible = ""

    has_reasoning = bool(
        message.get("reasoning")
        or message.get("reasoning_content")
        or message.get("reasoning_details")
        or (content and THINK_RE.search(content))
    )

    has_tool_calls = bool(message.get("tool_calls"))

    return has_reasoning and not visible and not has_tool_calls


# ---------------------------------------------------------------------------
# Content-with-tools fallback
# ---------------------------------------------------------------------------


class ContentWithToolsCache:
    """Caches text content from turns that had both content AND tool calls.

    If a subsequent turn produces only thinking (no visible content), the
    cached content can be used as the final response instead of retrying.
    This avoids wasting iterations when the model has already produced a
    useful response.
    """

    def __init__(self) -> None:
        self._cached: str | None = None

    def maybe_cache(self, message: dict[str, Any]) -> None:
        """Cache content if the message has both content and tool calls."""
        content = (message.get("content") or "").strip()
        tool_calls = message.get("tool_calls")
        if content and tool_calls:
            self._cached = content

    def get_fallback(self) -> str | None:
        """Return the cached content, or ``None`` if nothing is cached."""
        return self._cached

    def clear(self) -> None:
        """Clear the cache (e.g. on a normal response without tool calls)."""
        self._cached = None


# ---------------------------------------------------------------------------
# Incomplete scratchpad detection
# ---------------------------------------------------------------------------


def has_incomplete_scratchpad(message: dict[str, Any]) -> bool:
    """Return ``True`` if the content contains an unclosed ``<REASONING_SCRATCHPAD>`` tag.

    This happens when the model runs out of tokens mid-reasoning.  The
    caller should retry (up to a limit) since the model often completes
    the scratchpad on a second attempt.
    """
    content = message.get("content")
    if not content or not isinstance(content, str):
        return False

    opens = len(SCRATCHPAD_OPEN_RE.findall(content))
    closes = len(SCRATCHPAD_CLOSE_RE.findall(content))

    return opens > closes


# ---------------------------------------------------------------------------
# Thinking budget exhaustion
# ---------------------------------------------------------------------------


def is_thinking_budget_exhausted(message: dict[str, Any]) -> bool:
    """Return ``True`` if a ``finish_reason="length"`` response spent all tokens on thinking.

    When the model uses its entire output budget on reasoning and produces
    no visible response content, continuation retries are pointless --
    the model will just think more without answering.

    Checks whether any content remains *after* stripping ``<think>`` /
    ``<thinking>`` blocks.  Also returns ``True`` when content is ``None``
    (i.e., the model produced nothing at all despite hitting the length
    limit, which means reasoning consumed everything).
    """
    content = message.get("content")
    if content is None:
        return True
    if not isinstance(content, str):
        return False
    visible = THINK_RE.sub("", content).strip()
    return not visible
