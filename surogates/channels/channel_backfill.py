"""Channel history backfill — pure core (filter, bound, format).

No I/O here: given raw platform messages + channel metadata + limits, produce
the single context block seeded into a channel session. Slack fetching, caching,
and session seeding live in the platform and coordinator layers.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from surogates.session.events import EventType


@dataclass(frozen=True)
class BackfillLimits:
    max_messages: int = 200
    max_tokens: int = 8000
    max_age_days: int = 7
    max_pages: int = 1
    fetch_time_budget_s: float = 5.0
    cache_ttl_s: int = 3600
    negative_cooldown_s: int = 600

    @classmethod
    def from_config(cls, cfg: dict | None) -> "BackfillLimits":
        cfg = cfg or {}
        base = dataclasses.asdict(cls())
        for k in base:
            if k in cfg and cfg[k] is not None:
                base[k] = type(base[k])(cfg[k])
        return cls(**base)


@dataclass(frozen=True)
class RawMessage:
    ts: float
    author: str
    text: str


@dataclass(frozen=True)
class ChannelMeta:
    name: str
    topic: str
    purpose: str


def filter_messages(messages: list[dict], *, bot_user_id: str) -> list[dict]:
    """Drop own-bot messages, other bots, and any subtyped system message."""
    out: list[dict] = []
    for m in messages:
        if bot_user_id and m.get("user") == bot_user_id:
            continue
        if m.get("bot_id") or m.get("subtype"):
            continue
        if not (m.get("text") or "").strip():
            continue
        out.append(m)
    return out


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def bound_messages(
    messages: list[RawMessage], limits: BackfillLimits, *, now: float
) -> list[RawMessage]:
    """Take newest-first messages, apply age/count/token caps, return oldest-first."""
    oldest_allowed = now - limits.max_age_days * 86400.0
    picked: list[RawMessage] = []
    tokens = 0
    for m in messages:  # newest-first
        if m.ts < oldest_allowed:
            break
        cost = _est_tokens(m.text) + _est_tokens(m.author) + 8  # +label overhead
        # The newest message is always included (picked is empty on the first iteration) so a session never gets an empty block when history exists, even if that one message exceeds max_tokens.
        if picked and tokens + cost > limits.max_tokens:
            break
        if len(picked) >= limits.max_messages:
            break
        picked.append(m)
        tokens += cost
    picked.reverse()  # oldest-to-newest
    return picked


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_context_block(
    meta: ChannelMeta, messages: list[RawMessage], *, now: float
) -> str | None:
    if not messages:
        return None
    lines = ["[channel context - history before the agent joined]"]
    lines.append(f"Channel: #{meta.name}" if meta.name else "Channel: (unnamed)")
    if meta.topic:
        lines.append(f"Topic: {meta.topic}")
    if meta.purpose:
        lines.append(f"Purpose: {meta.purpose}")
    lines.append("")
    lines.append("Recent messages (oldest to newest, bounded):")
    for m in messages:
        lines.append(f"{_fmt_ts(m.ts)} {m.author}: {m.text}")
    lines.append("[/channel context]")
    return "\n".join(lines)


def cache_key(*, org_id: str, agent_id: str, kind: str, identifier: str, channel_id: str) -> str:
    return f"channel-backfill:{org_id}:{agent_id}:{kind}:{identifier}:{channel_id}"


async def read_block(redis, key: str) -> tuple[str, float] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        obj = json.loads(raw)
        return obj["block"], float(obj["fetched_at"])
    except (ValueError, KeyError, TypeError):
        return None


async def write_block(redis, key: str, block: str, *, fetched_at: float, ttl_s: int) -> None:
    await redis.set(key, json.dumps({"block": block, "fetched_at": fetched_at}), ex=ttl_s)


async def in_negative_cooldown(redis, key: str) -> bool:
    return bool(await redis.get(f"{key}:neg"))


async def mark_negative(redis, key: str, *, cooldown_s: int) -> None:
    await redis.set(f"{key}:neg", "1", ex=cooldown_s)


def is_stale(fetched_at: float, *, now: float, ttl_s: int) -> bool:
    return (now - fetched_at) >= ttl_s


_log = logging.getLogger(__name__)


async def warm_cache(
    *, platform, creds, redis, org_id, agent_id, identifier, channel_id,
    limits: BackfillLimits, now: float,
) -> bool:
    key = cache_key(org_id=org_id, agent_id=agent_id, kind="slack",
                    identifier=identifier, channel_id=channel_id)
    fetched = await platform.fetch_channel_context(
        creds=creds, channel_id=channel_id, limits=limits)
    if fetched is None:
        await mark_negative(redis, key, cooldown_s=limits.negative_cooldown_s)
        return False
    meta, raw = fetched
    block = format_context_block(meta, bound_messages(raw, limits, now=now), now=now)
    if not block:
        await mark_negative(redis, key, cooldown_s=limits.negative_cooldown_s)
        return False
    await write_block(redis, key, block, fetched_at=now, ttl_s=limits.cache_ttl_s)
    return True


@contextlib.asynccontextmanager
async def _session_lock(redis, session_id):
    """Best-effort short lock so concurrent first-messages for ONE session
    don't double-seed. A failed acquire yields False (caller skips)."""
    lock_key = f"channel-backfill:lock:{session_id}"
    acquired = await redis.set(lock_key, "1", ex=15, nx=True)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                await redis.delete(lock_key)


async def _already_seeded(store, session_id) -> bool:
    get_session = getattr(store, "get_session", None)
    if get_session is not None:
        session = await get_session(session_id)
        config = getattr(session, "config", None) or {}
        if config.get("history_backfill"):
            return True
    prior = await store.get_events(session_id, types=[EventType.USER_MESSAGE])
    for e in prior:
        data = getattr(e, "data", None) or {}
        if data.get("synthetic") == "channel_history_backfill":
            return True
        if not data.get("synthetic"):
            return True  # a real user message already exists — too late to seed
    return False


async def maybe_seed_session(
    *, store, redis, platform, creds, routing, session_id, channel_id,
    limits: BackfillLimits, now: float,
) -> int | None:
    """Seed one channel session with the cached/freshly-fetched context block.

    Best-effort: returns the seeded event id, or None when skipped/failed.
    Never raises — a backfill failure must not block the user's real message.
    """
    try:
        key = cache_key(org_id=routing.org_id, agent_id=routing.agent_id, kind="slack",
                        identifier=routing.identifier, channel_id=channel_id)
        async with _session_lock(redis, session_id) as got:
            if not got:
                return None
            if await _already_seeded(store, session_id):
                return None
            cached = await read_block(redis, key)
            if cached is not None and not is_stale(cached[1], now=now, ttl_s=limits.cache_ttl_s):
                block, fetched_at = cached
            else:
                if await in_negative_cooldown(redis, key):
                    return None
                ok = await warm_cache(
                    platform=platform, creds=creds, redis=redis,
                    org_id=routing.org_id, agent_id=routing.agent_id,
                    identifier=routing.identifier, channel_id=channel_id,
                    limits=limits, now=now)
                if not ok:
                    return None
                refreshed = await read_block(redis, key)
                if refreshed is None:
                    return None
                block, fetched_at = refreshed
            event_id = await store.emit_synthetic_user_message(
                session_id, content=block, synthetic="channel_history_backfill",
                metadata={"source": {
                    "platform": "slack", "chat_id": channel_id,
                    "channel_history_backfill": True, "cache_fetched_at": fetched_at,
                }})
            await store.update_session_config_key(
                session_id, "history_backfill",
                {"seeded_at": now, "event_id": event_id, "cache_fetched_at": fetched_at})
            return event_id
    except Exception:
        _log.warning("maybe_seed_session failed for %s", channel_id, exc_info=True)
        return None
