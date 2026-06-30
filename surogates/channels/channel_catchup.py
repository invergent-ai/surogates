"""Boot-time catch-up: replay Slack messages missed while the platform was down.

On channels-process startup, for each Slack app the bot is provisioned in, list
the bot's conversations and replay any human messages newer than the last one we
processed (the watermark) through the normal inbound pipeline. Bounded by
``BackfillLimits``; silent; best-effort; safe to run on every restart.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from surogates.channels.channel_backfill import BackfillLimits, filter_messages
from surogates.channels.credentials import resolve_channel_credentials
from surogates.runtime.leader_lock import RedisLeaderLock
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


_WATERMARK_SQL = text("""
    SELECT COALESCE(
        e.data #>> '{source,ts}',
        to_char(EXTRACT(EPOCH FROM e.created_at AT TIME ZONE 'UTC'), 'FM9999999990.000000')
    ) AS watermark
    FROM events e
    JOIN sessions s ON s.id = e.session_id
    WHERE e.org_id = :org_id
      AND s.agent_id = :agent_id
      AND e.type = :event_type
      AND e.data #>> '{source,platform}' = 'slack'
      AND e.data #>> '{source,api_app_id}' = :api_app_id
      AND e.data #>> '{source,chat_id}' = :chat_id
      AND NOT (e.data ? 'synthetic')
    ORDER BY watermark DESC
    LIMIT 1
""")


def _watermark_from(source_ts: str | None, created_at: datetime | None) -> str | None:
    """Pick the catch-up watermark for a conversation.

    Prefers the exact stored Slack ``source.ts`` string; falls back to the latest
    event's ``created_at`` rendered as a Slack-style ts (compatibility bridge for
    events stored before ``source.ts`` existed); ``None`` when we have never
    processed the conversation (first-run guard).
    """
    if source_ts:
        return source_ts
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return f"{created_at.timestamp():.6f}"
    return None


async def latest_catchup_watermark(
    session_factory: Any,
    *,
    org_id: Any,
    agent_id: str,
    api_app_id: str,
    chat_id: str,
) -> str | None:
    """Latest Slack ts we have processed for (org, agent, Slack app, conversation).

    Slack ``ts`` is compared/selected as a string (fixed ``seconds.microseconds``
    shape) — never converted to float. Returns ``None`` when there is no
    non-synthetic ``USER_MESSAGE`` for the conversation.
    """
    async with session_factory() as db:
        result = await db.execute(
            _WATERMARK_SQL,
            {
                "org_id": org_id,
                "agent_id": agent_id,
                "event_type": EventType.USER_MESSAGE.value,
                "api_app_id": api_app_id,
                "chat_id": chat_id,
            },
        )
        watermark = result.scalar_one_or_none()
    return str(watermark) if watermark is not None else None


class _Routing:
    """Minimal routing carrier (same shape the dispatcher builds)."""

    def __init__(self, *, org_id: str, agent_id: str, platform: str, identifier: str, config: dict) -> None:
        self.org_id = org_id
        self.agent_id = agent_id
        self.platform = platform
        self.identifier = identifier
        self.config = config


class ChannelCatchup:
    """Replays Slack messages missed during downtime, on channels-process boot."""

    _CONV_TYPES = "public_channel,private_channel,im,mpim"

    def __init__(
        self,
        *,
        redis: Any,
        session_factory: Any,
        vault: Any,
        platform_client: Any,
        registry: Any,
        pipeline: Any,
        deps_factory: Any,
        settings: Any,
        limits: BackfillLimits | None = None,
        pace_s: float = 0.5,
        lock: Any = None,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._vault = vault
        self._platform_client = platform_client
        self._registry = registry
        self._pipeline = pipeline
        self._deps_factory = deps_factory
        self._settings = settings
        self._limits = limits or BackfillLimits()
        self._pace_s = pace_s
        self._lock = lock  # per-app leader lock; None = run unguarded

    # -- top level ----------------------------------------------------------

    async def run(self) -> None:
        """Best-effort: catch up every provisioned Slack app. Never raises."""
        try:
            apps = await self._platform_client.list_channel_routings("slack")
        except Exception:
            logger.warning("[catchup] could not list slack routings", exc_info=True)
            return
        for app in apps:
            try:
                await self._catchup_app(app)
            except Exception:
                logger.warning(
                    "[catchup] app %s failed", app.get("channel_identifier"), exc_info=True,
                )

    async def _catchup_app(self, app: dict) -> None:
        app_id: str = app.get("channel_identifier", "")
        org_id: str = app.get("org_id", "")
        agent_id: str = app.get("agent_id", "")
        if not (app_id and org_id and agent_id):
            return
        lock = self._make_lock(app_id)
        if not await lock.acquire():
            logger.info("[catchup] app %s locked by another instance — skipping", app_id)
            return
        try:
            platform = self._resolve_platform(app)
            if platform is None:
                return
            creds = await resolve_channel_credentials(
                vault=self._vault, kind="slack", identifier=app_id, org_id=org_id,
                refs=platform.descriptor.vault_refs(app_id),
            )
            if not (creds or {}).get("bot_token"):
                return
            routing = _Routing(
                org_id=org_id, agent_id=agent_id, platform="slack",
                identifier=app_id, config=app.get("config") or {},
            )
            for conv_id, channel_type in await self._list_conversations(platform, creds):
                try:
                    await self._catchup_conversation(
                        platform=platform, routing=routing, creds=creds,
                        conv_id=conv_id, channel_type=channel_type,
                    )
                except Exception:
                    logger.warning("[catchup] conversation %s failed", conv_id, exc_info=True)
                if not await lock.heartbeat():
                    logger.warning("[catchup] app %s lost leader lock — stopping", app_id)
                    return
        finally:
            await lock.release()

    # -- per conversation ---------------------------------------------------

    async def _catchup_conversation(
        self, *, platform: Any, routing: _Routing, creds: dict, conv_id: str, channel_type: str,
    ) -> None:
        wm = await self._watermark(
            org_id=routing.org_id, agent_id=routing.agent_id,
            api_app_id=routing.identifier, chat_id=conv_id,
        )
        if wm is None:
            return  # first-run guard: establish on the next live message, replay nothing
        raw = await self._fetch_history(platform, creds, conv_id, oldest=wm)
        # Pre-filter obvious bot/subtype/empty events. Own-bot bare messages are
        # still filtered by platform.parse(creds=...) using the cached bot user id,
        # matching the live dispatcher path.
        kept = [m for m in filter_messages(raw, bot_user_id="")
                if str(m.get("ts", "")) > wm]
        kept.sort(key=lambda m: str(m.get("ts", "")))      # ascending
        kept = self._bound_messages(kept)
        for m in kept:
            await self._replay(platform, routing, creds, conv_id, channel_type, m)
            if self._pace_s:
                await _sleep(self._pace_s)

    async def _replay(
        self, platform: Any, routing: _Routing, creds: dict, conv_id: str, channel_type: str, m: dict,
    ) -> None:
        body = {
            "api_app_id": routing.identifier,
            "event": {
                "type": "message",
                "channel": conv_id,
                "channel_type": channel_type,
                "user": m.get("user", ""),
                "text": m.get("text", ""),
                "ts": str(m.get("ts", "")),
                **({"thread_ts": m["thread_ts"]} if m.get("thread_ts") else {}),
                **({"files": m["files"]} if m.get("files") else {}),
            },
        }
        msg = await platform.parse(body, creds=creds, identifier=routing.identifier)
        if msg is None:
            return
        msg = await platform.enrich(msg, creds=creds)
        deps = self._deps_factory(platform.kind, routing, creds, platform)
        await self._pipeline.handle(msg, routing=routing, config=routing.config, deps=deps)

    # -- slack fetch helpers (overridable in tests) -------------------------

    def _resolve_platform(self, app: dict) -> Any:
        return self._registry.get("slack") if self._registry is not None else None

    def _make_lock(self, app_id: str) -> Any:
        if self._lock is not None:
            return self._lock  # injected (tests)
        return RedisLeaderLock(
            self._redis,
            key=f"channel-catchup:leader:{app_id}",
            ttl_seconds=120,
            holder_id=uuid.uuid4().hex,
        )

    async def _watermark(self, **kw: Any) -> str | None:
        return await latest_catchup_watermark(self._session_factory, **kw)

    async def _list_conversations(self, platform: Any, creds: dict) -> list[tuple[str, str]]:
        client = platform._get_client((creds or {}).get("bot_token") or "")
        out: list[tuple[str, str]] = []
        cursor = ""
        while True:
            resp = await client.conversations_list(
                types=self._CONV_TYPES, exclude_archived=True, limit=200,
                **({"cursor": cursor} if cursor else {}),
            )
            for ch in resp.get("channels") or []:
                cid = ch.get("id")
                if not cid:
                    continue
                if ch.get("is_im"):
                    channel_type = "im"
                elif ch.get("is_mpim"):
                    channel_type = "mpim"
                elif ch.get("is_private"):
                    channel_type = "group"
                else:
                    channel_type = "channel"
                # channels/groups: only those the bot is a member of
                if channel_type in ("channel", "group") and not ch.get("is_member"):
                    continue
                out.append((cid, channel_type))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return out

    async def _fetch_history(self, platform: Any, creds: dict, conv_id: str, *, oldest: str) -> list[dict]:
        client = platform._get_client((creds or {}).get("bot_token") or "")
        floor = self._age_floor(oldest)
        msgs: list[dict] = []
        cursor = ""
        for _ in range(max(1, self._limits.max_pages)):
            resp = await client.conversations_history(
                channel=conv_id, oldest=floor, limit=200,
                **({"cursor": cursor} if cursor else {}),
            )
            msgs.extend(resp.get("messages") or [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor or len(msgs) >= self._limits.max_messages:
                break
        return msgs

    def _age_floor(self, watermark: str) -> str:
        """Clamp the fetch start to the BackfillLimits age window."""
        cutoff = _now() - self._limits.max_age_days * 86400.0
        cutoff_ts = f"{cutoff:.6f}"
        return watermark if watermark > cutoff_ts else cutoff_ts

    def _bound_messages(self, messages: list[dict]) -> list[dict]:
        """Apply the catch-up count and token caps to oldest-first dicts."""
        picked: list[dict] = []
        tokens = 0
        for m in messages:
            file_bits = " ".join(
                " ".join(str(f.get(k) or "") for k in ("id", "name"))
                for f in (m.get("files") or [])
            )
            body = " ".join(part for part in ((m.get("text") or ""), file_bits) if part)
            cost = max(1, len(body) // 4) + 8
            if picked and tokens + cost > self._limits.max_tokens:
                break
            if len(picked) >= self._limits.max_messages:
                break
            picked.append(m)
            tokens += cost
        return picked


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _now() -> float:
    return time.time()
