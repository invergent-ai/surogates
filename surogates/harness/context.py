"""Context compressor -- manages conversation compression when approaching context limits.

When the accumulated token count of messages approaches the model's context
window the compressor applies a multi-stage reduction:

1. **Tool-result pruning** -- old tool results are replaced with truncation
   markers so the LLM still sees the structure of prior tool calls without
   the (often large) payloads.
2. **Tail protection** -- the most-recent messages within a configurable
   token budget are left untouched so the LLM retains fresh working memory.
3. **Middle summarisation** -- the block of messages between the protected
   tail and the system prompt is compressed via an LLM summarisation call.

The result is a shorter message list plus a ``summary_data`` dict suitable
for persisting as a ``CONTEXT_COMPACT`` event.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.harness.model_metadata import (
    ModelInfo,
    estimate_tokens,
    get_model_info,
)

logger = logging.getLogger(__name__)

# Fallback context window when the model is unknown.
_DEFAULT_CONTEXT_WINDOW: int = 128_000

# Per-message overhead (role, name, separators) expressed in estimated tokens.
_PER_MESSAGE_OVERHEAD: int = 4

# Truncation marker injected in place of old tool results.
_TRUNCATED_MARKER: str = "[result truncated]"


class ContextCompressor:
    """Compresses conversation history when approaching the model's context window limit."""

    def __init__(
        self,
        model_id: str,
        *,
        compression_threshold: float = 0.75,
        tail_protection_tokens: int = 4000,
    ) -> None:
        if not 0.0 < compression_threshold < 1.0:
            raise ValueError("compression_threshold must be in (0, 1)")
        self._model_id = model_id
        info: ModelInfo | None = get_model_info(model_id)
        self._context_window: int = info.context_window if info else _DEFAULT_CONTEXT_WINDOW
        self._threshold = compression_threshold
        self._tail_protection_tokens = tail_protection_tokens

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_compress(self, messages: list[dict], system_prompt: str) -> bool:
        """Check if messages + system prompt exceed the compression threshold."""
        total = estimate_tokens(system_prompt) + self._estimate_messages_tokens(messages)
        limit = int(self._context_window * self._threshold)
        return total > limit

    async def compress(
        self,
        messages: list[dict],
        llm_client: Any,
    ) -> tuple[list[dict], dict]:
        """Compress *messages*.

        Returns ``(compressed_messages, summary_data)`` where
        *summary_data* is a dict suitable for the ``CONTEXT_COMPACT`` event
        payload.

        Strategy
        --------
        1. Prune old tool results (replace with ``"[result truncated]"``).
        2. Protect the tail (last N tokens of conversation).
        3. Summarise the middle section via LLM.
        4. Return compressed messages + summary dict.
        """
        original_count = len(messages)
        original_tokens = self._estimate_messages_tokens(messages)

        # Stage 1 -- prune tool results.
        pruned = self._prune_tool_results(messages)

        # If pruning alone is sufficient, skip the expensive LLM call.
        if not self._over_threshold(pruned):
            logger.info(
                "Context compression: tool-result pruning sufficient "
                "(%d -> %d est. tokens)",
                original_tokens,
                self._estimate_messages_tokens(pruned),
            )
            return pruned, self._build_summary_data(
                original_count=original_count,
                original_tokens=original_tokens,
                compressed_count=len(pruned),
                compressed_tokens=self._estimate_messages_tokens(pruned),
                strategy="prune_tool_results",
            )

        # Stage 2 -- split into head (to summarise) and tail (to protect).
        head, tail = self._split_tail(pruned)

        if len(head) <= 1:
            # Nothing meaningful to summarise; return as-is.
            return pruned, self._build_summary_data(
                original_count=original_count,
                original_tokens=original_tokens,
                compressed_count=len(pruned),
                compressed_tokens=self._estimate_messages_tokens(pruned),
                strategy="prune_only_head_too_small",
            )

        # Stage 3 -- summarise the head block via LLM.
        summary_text = await self._generate_summary(head, llm_client, self._model_id)

        compressed: list[dict] = [
            {"role": "user", "content": f"[Prior conversation summary]\n{summary_text}"},
            {
                "role": "assistant",
                "content": (
                    "Understood. I have the context from the conversation "
                    "summary above."
                ),
            },
            *tail,
        ]

        compressed_tokens = self._estimate_messages_tokens(compressed)
        logger.info(
            "Context compression: summarised %d messages (%d tokens) "
            "-> %d messages (%d tokens)",
            original_count,
            original_tokens,
            len(compressed),
            compressed_tokens,
        )

        return compressed, self._build_summary_data(
            original_count=original_count,
            original_tokens=original_tokens,
            compressed_count=len(compressed),
            compressed_tokens=compressed_tokens,
            strategy="summarise",
            summary=summary_text,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_tool_results(
        self,
        messages: list[dict],
        max_age_messages: int = 20,
    ) -> list[dict]:
        """Replace old tool results with truncation markers.

        Messages whose index is more than *max_age_messages* from the end
        and whose role is ``"tool"`` have their ``content`` replaced.
        """
        if len(messages) <= max_age_messages:
            return list(messages)

        cutoff = len(messages) - max_age_messages
        pruned: list[dict] = []
        for idx, msg in enumerate(messages):
            if idx < cutoff and msg.get("role") == "tool":
                pruned.append({**msg, "content": _TRUNCATED_MARKER})
            else:
                pruned.append(msg)
        return pruned

    async def _generate_summary(
        self,
        messages: list[dict],
        llm_client: Any,
        model_id: str,
    ) -> str:
        """Ask the LLM to summarise a block of messages."""
        serialised = self._serialise_for_summary(messages)

        system = (
            "You are a context-compression assistant. Summarise the following "
            "conversation excerpt preserving all key facts, decisions, code "
            "snippets, file paths, variable names, and tool-call outcomes. "
            "Be concise but do not lose any information the assistant would "
            "need to continue the task."
        )

        # Use a cheaper / smaller model variant when available; fall back to
        # the session model.  gpt-4o-mini is a good default summariser.
        summariser_model = "gpt-4o-mini"
        info = get_model_info(summariser_model)
        if info is None:
            summariser_model = model_id

        response = await llm_client.chat.completions.create(
            model=summariser_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": serialised},
            ],
            temperature=0.2,
            max_tokens=2048,
        )
        return response.choices[0].message.content or ""

    def _estimate_messages_tokens(self, messages: list[dict]) -> int:
        """Rough token estimate for a message list."""
        total = 0
        for msg in messages:
            total += _PER_MESSAGE_OVERHEAD
            content = msg.get("content")
            if isinstance(content, str):
                total += estimate_tokens(content)
            elif isinstance(content, list):
                # Multimodal content blocks.
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += estimate_tokens(block["text"])
                    else:
                        # Image or other non-text block -- rough constant.
                        total += 256
            # Tool calls embedded in assistant messages.
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    total += estimate_tokens(fn.get("name", ""))
                    total += estimate_tokens(fn.get("arguments", ""))
        return total

    def _over_threshold(self, messages: list[dict]) -> bool:
        """Check if *messages* alone exceed the compression threshold."""
        total = self._estimate_messages_tokens(messages)
        limit = int(self._context_window * self._threshold)
        return total > limit

    def _split_tail(
        self,
        messages: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Split messages into ``(head, tail)`` protecting the tail.

        Walks backwards from the end accumulating tokens until
        ``tail_protection_tokens`` is reached, then splits.
        """
        tail_budget = self._tail_protection_tokens
        accumulated = 0
        split_idx = len(messages)

        for i in range(len(messages) - 1, -1, -1):
            content = messages[i].get("content", "")
            if not isinstance(content, str):
                content = ""
            msg_tokens = _PER_MESSAGE_OVERHEAD + estimate_tokens(content)
            if accumulated + msg_tokens > tail_budget:
                break
            accumulated += msg_tokens
            split_idx = i

        # Ensure we always keep at least 1 message in the head.
        if split_idx == 0:
            split_idx = 1

        return messages[:split_idx], messages[split_idx:]

    @staticmethod
    def _serialise_for_summary(messages: list[dict]) -> str:
        """Flatten messages into a readable text block for summarisation."""
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and "text" in b
                ]
                content = "\n".join(text_parts)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                tc_summary = "; ".join(
                    f'{tc.get("function", {}).get("name", "?")}'
                    f'({tc.get("function", {}).get("arguments", "")[:200]})'
                    for tc in tool_calls
                )
                lines.append(f"[{role}] (tool calls: {tc_summary})")
            elif content:
                # Cap individual message content to avoid blowing up the
                # summarisation prompt.
                if len(content) > 4000:
                    content = content[:4000] + "...[truncated]"
                lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    @staticmethod
    def _build_summary_data(
        *,
        original_count: int,
        original_tokens: int,
        compressed_count: int,
        compressed_tokens: int,
        strategy: str,
        summary: str | None = None,
    ) -> dict:
        """Build the data payload for a ``CONTEXT_COMPACT`` event."""
        data: dict[str, Any] = {
            "original_message_count": original_count,
            "original_token_estimate": original_tokens,
            "compressed_message_count": compressed_count,
            "compressed_token_estimate": compressed_tokens,
            "strategy": strategy,
        }
        if summary is not None:
            data["summary"] = summary
        return data
