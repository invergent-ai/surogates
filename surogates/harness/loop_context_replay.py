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
from surogates.harness.loop_tool_recovery import collapse_repeated_tool_rounds
from surogates.harness.sanitize import strip_budget_warnings
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


def build_user_message_dict(
    event_data: dict,
    *,
    base_content: str | None = None,
) -> dict:
    """Construct the replayed LLM user message for one ``user.message`` event.

    Folds the per-turn ephemeral context — inlined attachment content,
    view-context note, path-only attachment note, and image vision blocks —
    onto the user's text, exactly as the conversation history is rebuilt.

    ``base_content`` overrides the event's own ``content``.  The slash-skill
    and ``/deep-research`` rewrite paths pass the expanded directive here so
    the skill/delegation body replaces the raw ``/command`` text while the
    attachment binding for *this* turn survives.  Without that, the rewrite
    discarded the note/inlined content and the model bound the request to an
    earlier upload still visible in history instead of the file the user just
    attached.
    """
    content = base_content if base_content is not None else event_data.get("content", "")
    content = _render_inlined_attachments(content, event_data.get("attachments"))
    # Fold per-user ephemeral notes (view-context, non-inlined attachments)
    # into the user content here so the bytes are determined entirely by the
    # durable event payload.  This keeps the provider's implicit prefix cache
    # stable across turns -- the previous design inserted the notes mid-array
    # before the latest user message, which left them present in turn T's
    # request but absent in turn T+1's prefix.
    note_parts: list[str] = []
    view_note = _view_context_note_from_metadata(event_data.get("metadata"))
    if view_note:
        note_parts.append(view_note)
    attachments_note = _attachments_note_from_data(event_data)
    if attachments_note:
        note_parts.append(attachments_note)
    if note_parts:
        notes_block = "\n\n".join(note_parts)
        content = f"{notes_block}\n\n{content}" if content else notes_block

    images = event_data.get("images")
    if images:
        logger.info(
            "User message has %d image(s), first mime: %s",
            len(images),
            images[0].get("mime_type", "?"),
        )
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
        return user_msg
    return {"role": "user", "content": content}


def coalesce_user_messages(messages: list[dict]) -> dict:
    """Merge one or more rendered user-message dicts into a single user turn.

    Both the live boundary injector and the replay re-sequencer pass the
    same rendered messages here so a steered turn looks byte-identical
    whether it was injected live or reconstructed from the event log.

    Text-only messages join with a blank-line separator. If any message
    is multimodal (its ``content`` is a block list), the result is a
    single block list preserving every text and image block in order.
    """
    if len(messages) == 1:
        return messages[0]

    if any(isinstance(m.get("content"), list) for m in messages):
        blocks: list[dict] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                blocks.extend(content)
            elif content:
                blocks.append({"type": "text", "text": content})
        return {"role": "user", "content": blocks}

    text = "\n\n".join(m.get("content") or "" for m in messages if m.get("content"))
    return {"role": "user", "content": text}


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

        Mid-turn steering: a real ``user.message`` can land in the log
        while an LLM iteration is still open (mid-stream, or while its
        tool calls are running), because the API appends it the instant
        it arrives.  Such a message is deferred to the iteration's close
        and coalesced, so the rebuilt order matches the live
        boundary-injection order and never splits a tool-call / tool
        result pair.  ``tool.result`` events carry no iteration marker,
        so an open tool-calling iteration is closed by tracking the
        ``tool_calls[*].id`` set from its ``llm.response`` until every id
        has a matching result.
        """
        messages: list[dict] = []
        iteration_open = False
        awaiting_tool_ids: set[str] = set()
        deferred_users: list[dict] = []

        def _flush_deferred() -> None:
            nonlocal deferred_users
            if deferred_users:
                messages.append(coalesce_user_messages(deferred_users))
                deferred_users = []

        for event in events:
            etype = event.type

            if etype == EventType.LLM_REQUEST.value:
                iteration_open = True
                awaiting_tool_ids = set()

            elif etype == EventType.USER_MESSAGE.value:
                rendered = build_user_message_dict(event.data)
                if iteration_open and not (event.data or {}).get("synthetic"):
                    deferred_users.append(rendered)
                else:
                    messages.append(rendered)

            elif etype == EventType.LLM_RESPONSE.value:
                stored_message = event.data.get("message")
                if stored_message is not None:
                    messages.append(stored_message)
                tool_calls = (stored_message or {}).get("tool_calls") or []
                ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
                if ids:
                    awaiting_tool_ids = ids
                else:
                    iteration_open = False
                    awaiting_tool_ids = set()
                    _flush_deferred()

            elif etype == EventType.TOOL_RESULT.value:
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": event.data.get("content", ""),
                })
                if awaiting_tool_ids:
                    awaiting_tool_ids.discard(event.data.get("tool_call_id"))
                    if not awaiting_tool_ids:
                        iteration_open = False
                        _flush_deferred()

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
                    # The compacted snapshot already contains any earlier
                    # steered turn in its proper place; drop the buffer and
                    # close the window so it is not re-appended.
                    deferred_users = []
                    iteration_open = False
                    awaiting_tool_ids = set()

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

        # Flush any users deferred by an iteration that never closed (the
        # log ends mid-tool-execution because this is an in-progress wake).
        _flush_deferred()

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
