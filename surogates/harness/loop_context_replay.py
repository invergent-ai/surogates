"""Memory, message replay, and context engineering helpers for AgentHarness."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.harness.loop_attachments import build_user_message_dict
from surogates.harness.loop_tool_recovery import collapse_repeated_tool_rounds
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
                # Single source of truth for per-user-message construction
                # (attachment note + inlined file content + view-context note
                # + image blocks).  Shared with the slash-skill and
                # /deep-research rewrite paths in loop.py via
                # build_user_message_dict so the two can't drift -- that drift
                # was the original cause of slash turns dropping the current
                # turn's attachment + image context.  Building from the durable
                # event payload also keeps the request bytes (and the
                # provider's implicit prefix cache) stable across turns.
                messages.append(build_user_message_dict(event.data))

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

            elif (
                etype == EventType.BOARD_UPDATE.value
                and event.data.get("content")
            ):
                # Board snapshots/deltas re-enter the conversation exactly
                # as emitted: message bytes are determined by the durable
                # event payload, keeping the provider prefix cache
                # replay-stable.
                messages.append({
                    "role": "user",
                    "content": str(event.data["content"]),
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

            # Coding-agent run result — surface the final message to the
            # coordinator LLM so it can follow up on what /code did.  Progress
            # events are UI-only (not replayed); STARTED is bookkeeping.
            elif etype == EventType.CODE_RUN_RESULT.value:
                agent = event.data.get("agent", "coding agent")
                if event.data.get("error"):
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} failed: {event.data['error']}]",
                    })
                else:
                    final = event.data.get("final_message", "")
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} finished]\n{final}",
                    })

            # LLM_THINKING and LLM_DELTA are intentionally skipped.

        # Strip stale budget warnings from replayed tool results.
        strip_budget_warnings(messages)

        # Repair histories poisoned by a prior identical-call loop:
        # providers reject conversations that repeat the same tool call
        # across consecutive rounds, which would make every resume of
        # such a session fail with the same provider 400.
        return collapse_repeated_tool_rounds(messages)

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
