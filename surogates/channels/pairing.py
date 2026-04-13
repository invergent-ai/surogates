"""Channel pairing store — links platform users to Surogates accounts.

When an unknown user messages the bot on Slack/Teams/Telegram, the adapter
generates a short-lived pairing code and sends them a link to the web UI.
The user logs in, submits the code, and their platform identity is bound
to their Surogates account.

Pairing codes are stored in **Redis** with a configurable TTL (default 10
minutes).  This ensures the code is accessible from any API server replica
or channel adapter process.  Codes are single-use — resolved once, then
deleted.

Security features (based on Hermes pairing.py / OWASP / NIST SP 800-63-4):
- 8-char codes from 32-char unambiguous alphabet (no 0/O/1/I)
- Cryptographic randomness via ``secrets.choice()``
- Configurable TTL (default 10 minutes)
- Rate limiting: 1 request per user per 10 minutes
- Single-use: code is deleted on resolution
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

logger = logging.getLogger(__name__)

# Unambiguous alphabet — excludes 0/O, 1/I to prevent confusion.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

# Timing constants.
_DEFAULT_TTL = 600          # 10 minutes

# Redis key prefixes.
_PREFIX = "surogates:pairing:"
_RATE_PREFIX = "surogates:pairing_rate:"


def _generate_code() -> str:
    """Generate a human-friendly pairing code like ``A3F7-K9M2``."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


class PairingStore:
    """Redis-backed store for pending channel pairing codes.

    Works across multiple API server replicas and channel adapter processes.
    """

    def __init__(self, redis: Any, ttl: int = _DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    async def create(
        self,
        platform: str,
        platform_user_id: str,
        platform_meta: dict[str, Any] | None = None,
    ) -> str | None:
        """Generate a pairing code for a platform user.

        Returns the code string (e.g., ``"A3F7-K9M2"``), or ``None`` if
        the user is rate-limited.
        """
        # Rate limit: check if this user already has a pending code.
        # If the previous code is still alive, reuse it instead of generating
        # a new one.  If it expired, allow a fresh code immediately.
        rate_key = f"{_RATE_PREFIX}{platform}:{platform_user_id}"
        existing_code = await self._redis.get(rate_key)
        if existing_code:
            existing_code_str = existing_code.decode() if isinstance(existing_code, bytes) else str(existing_code)
            # Check if the code is still valid.
            if await self._redis.exists(f"{_PREFIX}{existing_code_str}"):
                return existing_code_str

        code = _generate_code()

        # Store the pairing entry in Redis with TTL.
        entry = json.dumps({
            "platform": platform,
            "platform_user_id": platform_user_id,
            "platform_meta": platform_meta or {},
        })
        redis_key = f"{_PREFIX}{code}"
        await self._redis.setex(redis_key, self._ttl, entry)

        # Record which code this user has (expires with the code).
        await self._redis.setex(rate_key, self._ttl, code)

        logger.info(
            "Pairing code created for %s:%s (TTL %ds)",
            platform, platform_user_id, self._ttl,
        )
        return code

    async def get(self, code: str) -> dict[str, Any] | None:
        """Look up a pairing code without consuming it.

        Returns ``{"platform": ..., "platform_user_id": ..., "platform_meta": ...}``
        or ``None`` if the code doesn't exist or has expired.
        """
        normalized = code.upper().strip()
        redis_key = f"{_PREFIX}{normalized}"
        raw = await self._redis.get(redis_key)
        if raw is None:
            return None
        return json.loads(raw)

    async def resolve(self, code: str) -> dict[str, Any] | None:
        """Consume a pairing code — returns the entry and deletes it.

        Returns ``None`` if the code doesn't exist or has expired.
        Single-use: calling resolve twice returns ``None`` the second time.
        """
        normalized = code.upper().strip()
        redis_key = f"{_PREFIX}{normalized}"

        # Atomic get-and-delete.
        raw = await self._redis.getdel(redis_key)
        if raw is None:
            return None

        entry = json.loads(raw)
        logger.info(
            "Pairing code resolved for %s:%s",
            entry.get("platform"), entry.get("platform_user_id"),
        )
        return entry
