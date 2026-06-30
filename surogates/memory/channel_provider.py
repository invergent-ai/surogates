"""ChannelMemoryProvider -- channel-scoped, context-only memory.

Registered as an INTERNAL provider (alongside the always-on builtin) so it
never displaces a tenant-selected external memory backend.  It exposes no
tools; it only ingests observed channel messages, recalls a context slice
at each wake via ``prefetch``, and harvests durable notes from messages
about to be compressed via ``on_pre_compress``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from surogates.memory.channel_store import ChannelMemoryStore
from surogates.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

# (observations, existing_notes) -> new_notes
Summarizer = Callable[[list[dict], str], str]

_RECALL_OBS_LIMIT = 30
_RECALL_SIGNAL_LIMIT = 10


class ChannelMemoryProvider(MemoryProvider):
    """Context-only memory for one shared channel."""

    def __init__(
        self,
        store: ChannelMemoryStore,
        *,
        channel_id: str,
        summarizer: Summarizer | None = None,
    ) -> None:
        self._store = store
        self._channel_id = channel_id
        self._summarizer = summarizer
        self._dirty = False

    @property
    def name(self) -> str:
        return "channel"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        # The store is loaded by the worker before the manager is built
        # (async), mirroring the R2 builtin store; nothing to do here.
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def system_prompt_block(self) -> str:
        return (
            f"You are the shared Surogate in channel {self._channel_id}. "
            "Your durable working memory of this channel is recalled below "
            "when relevant; treat it as continuity across everyone here."
        )

    # Ingestion (called by the worker after draining queued observations).

    def ingest(self, text: str, *, meta: dict) -> None:
        if not text or not text.strip():
            return
        self._store.append_observation(text.strip(), meta=meta)
        self._dirty = True

    # Recall.

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        parts: list[str] = []
        notes = self._store.notes()
        if notes.strip():
            parts.append(f"CHANNEL NOTES:\n{notes.strip()}")

        obs = self._store.recent_observations(limit=_RECALL_OBS_LIMIT)
        if obs:
            lines = []
            for o in obs:
                who = (o.get("meta") or {}).get("user_name") or (o.get("meta") or {}).get("user") or "someone"
                lines.append(f"- {who}: {o.get('text', '')}")
            parts.append("RECENT CHANNEL ACTIVITY:\n" + "\n".join(lines))

        sigs = self._store.signals(limit=_RECALL_SIGNAL_LIMIT)
        if sigs:
            slines = [f"- {s.get('title', s.get('external_id', 'signal'))}: {s.get('summary', '')}" for s in sigs]
            parts.append("RECENT TOOL SIGNALS:\n" + "\n".join(slines))

        return "\n\n".join(parts)

    # Compression harvest.

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        if self._summarizer is None:
            return ""
        try:
            obs = self._store.recent_observations(limit=_RECALL_OBS_LIMIT)
            new_notes = self._summarizer(obs, self._store.notes())
        except Exception:
            logger.debug("Channel summarizer failed", exc_info=True)
            return ""
        if new_notes and new_notes.strip():
            self._store.set_notes(new_notes.strip())
            self._dirty = True
            return new_notes.strip()
        return ""

    # Signals (consumed by the tool-signal plan).

    def append_signal(self, signal: dict) -> None:
        self._store.append_signal(signal)
        self._dirty = True

    def recent_signals(self, limit: int = 20) -> list[dict]:
        return self._store.signals(limit=limit)

    # Persistence.

    async def flush(self) -> None:
        if self._dirty:
            await self._store.flush()
            self._dirty = False

    def shutdown(self) -> None:
        # flush() is awaited by the worker on session teardown; nothing
        # blocking to do here.
        return None
