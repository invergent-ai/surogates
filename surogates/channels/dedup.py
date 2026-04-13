"""Shared helper classes for channel adapters.

Message deduplication — prevents reprocessing of events during
reconnections (Socket Mode, Telegram long-polling, etc.).
"""

from __future__ import annotations

import time
from typing import Dict


class MessageDeduplicator:
    """TTL-based message deduplication cache.

    Replaces the identical ``_seen_messages`` / ``_is_duplicate()`` pattern
    previously duplicated across multiple adapters.

    Usage::

        self._dedup = MessageDeduplicator()

        # In message handler:
        if self._dedup.is_duplicate(msg_id):
            return
    """

    def __init__(self, max_size: int = 2000, ttl_seconds: float = 300):
        self._seen: Dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        """Return True if *msg_id* was already seen within the TTL window."""
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return False

    def clear(self):
        """Clear all tracked messages."""
        self._seen.clear()
