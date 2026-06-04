"""Memory, message replay, and context engineering helpers for AgentHarness."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.harness.loop_attachments import (
    _attachments_note_from_data,
    _render_inlined_attachments,
)
from surogates.harness.loop_messages import _view_context_note_from_metadata
from surogates.harness.sanitize import strip_budget_warnings
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class ContextReplayMixin:
    async def _prefetch_memory(self, session_id: UUID) -> str | None:
        """Prefetch user memory and snapshot it for the session.

        The first wake() of a session reads memory from disk; every
        subsequent wake() reuses the cached snapshot byte-identically so
        the memory_context message stays in the provider's prefix cache.
        The snapshot is invalidated alongside the system prompt cache
        (compression / context overflow / explicit reset).

        If a MemoryManager is available, delegates to it and wraps the
        result in a ``<memory-context>`` fence.  Otherwise falls back to
        direct file I/O.
        """
        if session_id in self._memory_snapshot_cache:
            return self._memory_snapshot_cache[session_id]

        snapshot = await self._load_memory_snapshot()
        self._memory_snapshot_cache[session_id] = snapshot
        return snapshot

    async def _load_memory_snapshot(self) -> str | None:
        """Read the current memory context from disk (no caching)."""
        # Use memory manager if available.
        if self._memory_manager is not None:
            try:
                raw = self._memory_manager.prefetch_all("")
                if raw and raw.strip():
                    from surogates.memory.manager import build_memory_context_block
                    return build_memory_context_block(raw)
            except Exception:
                logger.debug("Memory manager prefetch failed", exc_info=True)
            return None

        # Fall back to direct file read.
        try:
            memory_dir = self._tenant.asset_root
            if not memory_dir:
                return None
            from pathlib import Path

            # Try user-scoped memory first, fall back to org shared
            for subdir in (
                f"users/{self._tenant.user_id}/memory",
                "shared/memory",
            ):
                memory_path = Path(memory_dir) / subdir / "MEMORY.md"
                if memory_path.is_file():
                    content = memory_path.read_text(encoding="utf-8").strip()
                    if content:
                        logger.debug("Prefetched memory from %s (%d chars)", memory_path, len(content))
                        return content
        except Exception:
            logger.debug("Memory prefetch failed", exc_info=True)
        return None

    def _rebuild_messages(self, events: list[Event]) -> list[dict]:
        """Replay event log to reconstruct conversation messages.

        Processes events in order.  A ``CONTEXT_COMPACT`` event replaces
        all previously accumulated messages with the compacted set stored
        in its data payload.

        ``LLM_THINKING`` events are **skipped** during replay -- they are
        informational only and should not re-enter the conversation.

        ``LLM_DELTA`` events are likewise skipped; the full response is
        captured in the subsequent ``LLM_RESPONSE`` event.
        """
        messages: list[dict] = []

        for event in events:
            etype = event.type

            if etype == EventType.USER_MESSAGE.value:
                content = event.data.get("content", "")
                content = _render_inlined_attachments(
                    content, event.data.get("attachments"),
                )
                # Fold per-user ephemeral notes (view-context, non-inlined
                # attachments) into the user content here so the bytes are
                # determined entirely by the durable event payload.  This
                # keeps the provider's implicit prefix cache stable across
                # turns -- the previous design inserted the notes mid-array
                # before the latest user message, which left them present
                # in turn T's request but absent in turn T+1's prefix.
                note_parts: list[str] = []
                view_note = _view_context_note_from_metadata(
                    event.data.get("metadata"),
                )
                if view_note:
                    note_parts.append(view_note)
                attachments_note = _attachments_note_from_data(event.data)
                if attachments_note:
                    note_parts.append(attachments_note)
                if note_parts:
                    notes_block = "\n\n".join(note_parts)
                    content = (
                        f"{notes_block}\n\n{content}" if content else notes_block
                    )
                images = event.data.get("images")
                if images:
                    logger.info(
                        "User message has %d image(s), first mime: %s",
                        len(images),
                        images[0].get("mime_type", "?"),
                    )
                if images:
                    blocks: list[dict] = [{"type": "text", "text": content}]
                    for img in images:
                        data_url = img["data"]
                        if not data_url.startswith("data:"):
                            mime = img.get("mime_type", "image/png")
                            data_url = f"data:{mime};base64,{data_url}"
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "auto"},
                        })
                    user_msg = {"role": "user", "content": blocks}
                    from surogates.harness.image_shrink import shrink_image_parts_in_messages
                    shrink_image_parts_in_messages([user_msg])
                    messages.append(user_msg)
                else:
                    messages.append({"role": "user", "content": content})

            elif etype == EventType.LLM_RESPONSE.value:
                stored_message = event.data.get("message")
                if stored_message is not None:
                    messages.append(stored_message)

            elif etype == EventType.TOOL_RESULT.value:
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": event.data.get("content", ""),
                })

            elif etype == EventType.ADVISOR_RESULT.value and event.data.get("content"):
                messages.append({
                    "role": "user",
                    "content": self._format_advisor_context(
                        category=event.data.get("category", "advisor"),
                        content=str(event.data.get("content") or ""),
                    ),
                })

            elif etype == EventType.CONTEXT_COMPACT.value:
                compacted = event.data.get("compacted_messages")
                if compacted is not None:
                    messages = list(compacted)

            # Worker coordination events — injected as synthetic user
            # messages so the coordinator LLM sees worker results.
            elif etype == EventType.WORKER_COMPLETE.value:
                worker_id = event.data.get("worker_id", "?")
                result = event.data.get("result", "")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} completed]\n{result}",
                })

            elif etype == EventType.WORKER_FAILED.value:
                worker_id = event.data.get("worker_id", "?")
                error = event.data.get("error", "unknown error")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} failed: {error}]",
                })

            # LLM_THINKING and LLM_DELTA are intentionally skipped.

        # Strip stale budget warnings from replayed tool results.
        strip_budget_warnings(messages)

        return messages

    # ------------------------------------------------------------------
    # Context engineering
    # ------------------------------------------------------------------

    async def _engineer_context(
        self,
        session: Session,
        events: list[Event],
        messages: list[dict],
    ) -> list[dict]:
        """Apply context compression if needed."""
        system_prompt = self._prompt.build()
        if not self._compressor.should_compress(messages, system_prompt):
            return messages

        compressed, summary_data = await self._compressor.compress(
            messages, self._llm,
        )

        await self._store.emit_event(
            session.id,
            EventType.CONTEXT_COMPACT,
            {
                **summary_data,
                "compacted_messages": compressed,
            },
        )

        # Invalidate system prompt cache -- conversation shape changed.
        self._system_prompt_cache.invalidate(session.id)
        self._memory_snapshot_cache.pop(session.id, None)

        return compressed

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    async def _build_system_prompt(self, session: Session) -> str:
        """Delegate to PromptBuilder, with per-session caching."""
        cached = self._system_prompt_cache.get(session.id)
        if cached is not None:
            return cached

        prompt = self._prompt.build()
        self._system_prompt_cache.set(session.id, prompt)
        return prompt
