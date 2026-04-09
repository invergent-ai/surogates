"""Credential pool for LLM provider resilience.

Manages multiple API credentials per provider. Rotates on rate limits (429),
billing exhaustion (402), and auth failures (401). Thread-safe.

Simplified from Hermes agent/credential_pool.py.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"

# Cooldown before retrying an exhausted credential
EXHAUSTED_TTL_429 = 3600       # 1 hour for rate limits
EXHAUSTED_TTL_DEFAULT = 86400  # 24 hours for other errors


@dataclass
class PooledCredential:
    id: str
    api_key: str
    base_url: str | None = None
    provider: str = "openai"
    label: str = ""
    priority: int = 0
    status: str = STATUS_OK
    status_at: float | None = None
    error_code: int | None = None
    error_message: str | None = None
    reset_at: float | None = None
    request_count: int = 0

    @property
    def runtime_api_key(self) -> str:
        return self.api_key

    @property
    def runtime_base_url(self) -> str | None:
        return self.base_url


class CredentialPool:
    def __init__(self, entries: list[PooledCredential]) -> None:
        self._entries = sorted(entries, key=lambda e: e.priority)
        self._current_idx: int = 0
        self._lock = threading.Lock()

    def current(self) -> PooledCredential | None:
        with self._lock:
            available = self._available_entries()
            if not available:
                return None
            if self._current_idx >= len(available):
                self._current_idx = 0
            return available[self._current_idx]

    def has_available(self) -> bool:
        with self._lock:
            return len(self._available_entries()) > 0

    def mark_exhausted_and_rotate(
        self,
        status_code: int,
        error_message: str = "",
        *,
        error_context: dict[str, Any] | None = None,
    ) -> PooledCredential | None:
        """Mark current credential as exhausted and rotate to the next available one.

        Parameters
        ----------
        status_code:
            HTTP status code that triggered the rotation (e.g. 429, 402, 401).
        error_message:
            A short human-readable error description (truncated to 500 chars).
        error_context:
            Structured error context dict (e.g. ``{"reason": "rate_limited",
            "reset_at": <epoch>}``).  When ``reset_at`` is present, the
            credential cooldown uses the provider-supplied reset time instead
            of the default TTL.
        """
        with self._lock:
            available = self._available_entries()
            if not available:
                return None
            if self._current_idx >= len(available):
                self._current_idx = 0
            current = available[self._current_idx]

            # Determine reset time from error_context if available.
            reset_at_override: float | None = None
            if isinstance(error_context, dict):
                raw_reset = error_context.get("reset_at")
                if raw_reset is not None:
                    try:
                        reset_at_override = float(raw_reset)
                    except (TypeError, ValueError):
                        pass

            default_ttl = EXHAUSTED_TTL_429 if status_code == 429 else EXHAUSTED_TTL_DEFAULT
            reset_at = reset_at_override if reset_at_override is not None else (time.time() + default_ttl)

            # Mark current as exhausted
            idx = self._entries.index(current)
            self._entries[idx] = replace(
                current,
                status=STATUS_EXHAUSTED,
                status_at=time.time(),
                error_code=status_code,
                error_message=error_message[:500] if error_message else None,
                reset_at=reset_at,
            )

            # Find next available
            available = self._available_entries()
            if not available:
                return None
            self._current_idx = 0
            return available[0]

    def try_refresh_current(self) -> PooledCredential | None:
        """Attempt to refresh the current credential (e.g. on 401).

        For pool-managed credentials, refreshing means resetting the status
        back to OK and bumping the request count.  This gives the credential
        another chance before rotating away from it.

        Returns the refreshed credential, or ``None`` if the current entry
        is already exhausted beyond recovery (i.e. no available entries).
        """
        with self._lock:
            available = self._available_entries()
            if not available:
                return None
            if self._current_idx >= len(available):
                self._current_idx = 0
            current = available[self._current_idx]

            # Reset status to OK -- the caller will rebuild the client
            # with the same key (after potential token refresh).
            idx = self._entries.index(current)
            refreshed = replace(
                current,
                status=STATUS_OK,
                status_at=time.time(),
                error_code=None,
                error_message=None,
                reset_at=None,
                request_count=current.request_count + 1,
            )
            self._entries[idx] = refreshed
            return refreshed

    def _available_entries(self) -> list[PooledCredential]:
        """Return entries not currently in exhaustion cooldown."""
        now = time.time()
        result: list[PooledCredential] = []
        for i, entry in enumerate(self._entries):
            if entry.status == STATUS_EXHAUSTED:
                if entry.reset_at and now < entry.reset_at:
                    continue
                # Cooldown expired -- reset to OK
                self._entries[i] = replace(
                    entry, status=STATUS_OK, status_at=None,
                    error_code=None, error_message=None, reset_at=None,
                )
                entry = self._entries[i]
            result.append(entry)
        return result
