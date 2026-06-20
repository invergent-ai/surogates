"""Cross-process browser metadata registry backed by a Redis hash."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis


REGISTRY_HASH_KEY = "surogates:browser:registry"


@dataclass(slots=True)
class BrowserEntry:
    """One browser metadata row keyed by session id."""

    session_id: str
    org_id: str
    user_id: str
    rest_url: str
    cdp_url: str
    live_view_url: str
    provisioned_at: datetime
    # Backend handle for the live browser (fleet pod / container id). Needed to
    # reattach to — or tear down — an existing browser from a worker that did
    # not provision it; defaulted for backwards-compat with older entries.
    browser_id: str = ""

    def to_json(self) -> str:
        payload = asdict(self)
        payload["provisioned_at"] = self.provisioned_at.isoformat()
        return json.dumps(payload)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "BrowserEntry":
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload: dict[str, Any] = json.loads(raw)
        return cls(
            session_id=payload["session_id"],
            org_id=payload["org_id"],
            user_id=payload["user_id"],
            rest_url=payload["rest_url"],
            cdp_url=payload["cdp_url"],
            live_view_url=payload["live_view_url"],
            provisioned_at=datetime.fromisoformat(payload["provisioned_at"]),
            browser_id=payload.get("browser_id", ""),
        )


class BrowserRegistry:
    """Async wrapper around browser metadata stored in Redis."""

    def __init__(self, redis: "Redis") -> None:
        self._redis = redis

    async def set(self, entry: BrowserEntry) -> None:
        await self._redis.hset(REGISTRY_HASH_KEY, entry.session_id, entry.to_json())

    async def get(self, session_id: str) -> BrowserEntry | None:
        raw = await self._redis.hget(REGISTRY_HASH_KEY, session_id)
        if raw is None:
            return None
        return BrowserEntry.from_json(raw)

    async def delete(self, session_id: str) -> None:
        await self._redis.hdel(REGISTRY_HASH_KEY, session_id)
