"""ChannelMemoryStore -- R2-backed durable memory for one shared channel.

Holds a single JSON document per channel: a rolling buffer of observed
messages (firehose), distilled notes, and normalized tool signals.  Reads are
sync (served from the in-memory working set so the prompt builder stays sync);
``load``/``flush`` are async (R2 I/O).

Mirrors :class:`surogates.memory.r2_store.R2MemoryStore`'s storage shape and
reuses the same ``memory_io`` read/write primitives so the on-the-wire object
is consistent with the rest of the memory subsystem.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from surogates.runtime.memory_io import read_user_memory, write_user_memory

__all__ = ["ChannelMemoryStore"]

logger = logging.getLogger(__name__)

_DOC_VERSION = 1


class ChannelMemoryStore:
    """Per-channel R2-backed memory document."""

    def __init__(
        self,
        *,
        backend: Any,
        bucket: str,
        key: str,
        max_observations: int = 200,
        max_signals: int = 100,
    ) -> None:
        self._backend = backend
        self._bucket = bucket
        self._key = key
        self._max_observations = max_observations
        self._max_signals = max_signals
        self._notes: str = ""
        self._observations: list[dict] = []
        self._signals: list[dict] = []
        self._last_seen_version: int = 0

    async def load(self) -> None:
        """Read the channel document from R2 into the working set."""
        result = await read_user_memory(
            self._backend, bucket=self._bucket, key=self._key,
        )
        if result is None:
            self._notes, self._observations, self._signals = "", [], []
            self._last_seen_version = 0
            return
        content, version = result
        self._last_seen_version = version
        try:
            doc = json.loads(content) if content else {}
        except json.JSONDecodeError:
            logger.warning("Channel memory doc at %s is corrupt; resetting", self._key)
            doc = {}
        self._notes = doc.get("notes", "") or ""
        self._observations = list(doc.get("observations", []))
        self._signals = list(doc.get("signals", []))

    # Sync reads / mutations on the working set.

    def append_observation(self, text: str, *, meta: dict) -> None:
        self._observations.append({"text": text, "meta": dict(meta)})
        if len(self._observations) > self._max_observations:
            self._observations = self._observations[-self._max_observations:]

    def recent_observations(self, limit: int = 50) -> list[dict]:
        return list(self._observations[-limit:])

    def notes(self) -> str:
        return self._notes

    def set_notes(self, text: str) -> None:
        self._notes = text or ""

    def append_signal(self, signal: dict) -> None:
        self._signals.append(dict(signal))
        if len(self._signals) > self._max_signals:
            self._signals = self._signals[-self._max_signals:]

    def signals(self, limit: int = 50) -> list[dict]:
        return list(self._signals[-limit:])

    # Async write.

    async def flush(self) -> None:
        """Serialize the working set and persist to R2 (last-write-wins)."""
        doc = {
            "version": _DOC_VERSION,
            "notes": self._notes,
            "observations": self._observations,
            "signals": self._signals,
        }
        content = json.dumps(doc, ensure_ascii=False)
        current = await read_user_memory(
            self._backend, bucket=self._bucket, key=self._key,
        )
        current_version = current[1] if current is not None else 0
        new_version = await write_user_memory(
            self._backend,
            bucket=self._bucket,
            key=self._key,
            content=content,
            expected_version=current_version,
        )
        self._last_seen_version = new_version
