"""Cross-process user-control flag for browser live view takeover."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


_KEY_PREFIX = "surogates:browser:control:"

# In a ``browser.control_returned`` of a chat on its user's computer (``computer: true``), which is
# told for the chat's pane, as the take-over was, and is no work for anyone:
#
# ``resumes: true`` says the hand back gave the chat's agent a turn: its user confirmed it on that
# computer, and the chat could take a turn then.  The turn itself is the ``session.resume`` written
# with it, whose source is BROWSER_HAND_BACK: that is what wakes the agent, and what it reads as
# the harness's note that it can go on (surogates.devices.browser.resumes_the_agent).
RESUMES = "resumes"
BROWSER_HAND_BACK = "browser_hand_back"
# ``handed_back_from`` names the chat the browser was handed back from, when it is not this one.
# The agent's browser there is one for all its chats, so a hand back made from one is told to each
# that still said its user held it.  It never resumes the chat it is told to.
HANDED_BACK_FROM = "handed_back_from"
# In a ``browser.control_granted`` of such a chat, ``taken_over_from`` names the chat the browser
# was taken over from, when it is not this one.  A chat whose hand back gave its agent a turn no
# request has read yet is told so: the browser is held again, and that turn is off.
TAKEN_OVER_FROM = "taken_over_from"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


@dataclass(slots=True)
class ControlEntry:
    """Current user-control holder for a browser session."""

    owner_user_id: str
    acquired_at: datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "owner_user_id": self.owner_user_id,
                "acquired_at": self.acquired_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ControlEntry":
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)
        return cls(
            owner_user_id=payload["owner_user_id"],
            acquired_at=datetime.fromisoformat(payload["acquired_at"]),
        )


def paused_by_user_result() -> str:
    """A browser tool's result while its user holds the browser: the cloud's live view, or a computer's own take-over."""
    return json.dumps(
        {
            "error": "paused_by_user",
            "guidance": (
                "The user has taken control of the browser. Wait for them to "
                "finish before continuing; every browser_* tool will return "
                "this error until they release control."
            ),
        }
    )


class AcquireOutcome(str, Enum):
    """Outcome of trying to acquire live browser control."""

    GRANTED = "granted"
    REFRESHED = "refreshed"
    CONFLICT = "conflict"


class BrowserControlStore:
    """Redis-backed lock that records user takeover of a browser session."""

    def __init__(self, redis: "Redis", *, ttl_seconds: int = 60) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def acquire(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[AcquireOutcome, ControlEntry]:
        entry = ControlEntry(
            owner_user_id=user_id,
            acquired_at=datetime.now(timezone.utc),
        )
        key = _key(session_id)
        acquired = await self._redis.set(
            key,
            entry.to_json(),
            nx=True,
            ex=self._ttl_seconds,
        )
        if acquired:
            return AcquireOutcome.GRANTED, entry

        existing = await self.get(session_id)
        if existing is None:
            return await self.acquire(session_id, user_id)
        if existing.owner_user_id != user_id:
            return AcquireOutcome.CONFLICT, existing

        await self._redis.set(key, entry.to_json(), ex=self._ttl_seconds)
        return AcquireOutcome.REFRESHED, entry

    async def release(self, session_id: str, user_id: str) -> bool:
        entry = await self.get(session_id)
        if entry is None or entry.owner_user_id != user_id:
            return False
        await self._redis.delete(_key(session_id))
        return True

    async def get(self, session_id: str) -> ControlEntry | None:
        raw = await self._redis.get(_key(session_id))
        if raw is None:
            return None
        return ControlEntry.from_json(raw)

    async def held_by(self, session_id: str) -> str | None:
        entry = await self.get(session_id)
        return entry.owner_user_id if entry is not None else None
