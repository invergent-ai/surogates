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
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from surogates.session.attachment_ingest import safe_display_name
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
    files: tuple[tuple[str, str], ...] = ()
    author_id: str = ""


@dataclass(frozen=True)
class ChannelMeta:
    name: str
    topic: str
    purpose: str


# Slack system / noise message subtypes — joins, topic changes, etc. — carry
# nothing an agent needs to read, so they are dropped on every path.
_SYSTEM_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
    "group_join", "group_leave", "group_topic", "group_purpose",
    "group_name", "group_archive", "group_unarchive",
    "pinned_item", "unpinned_item", "bot_add", "bot_remove", "tombstone",
})


def filter_messages(
    messages: list[dict], *, bot_user_id: str, drop_bots: bool = True
) -> list[dict]:
    """Filter raw Slack messages for agent consumption.

    Always drops the agent's own posts and Slack system messages (joins, topic
    changes, …). When *drop_bots* is True — the trigger/backfill path — it also
    drops every bot- or app-posted message and any subtyped message, so the
    agent never wakes on or is seeded with automated posts. When False — the
    on-demand ``fetch_channel_messages`` read path — bot/app posts are kept:
    reading a channel's daily report bots is the whole point of that tool.
    """
    out: list[dict] = []
    for m in messages:
        if bot_user_id and m.get("user") == bot_user_id:
            continue
        if (m.get("subtype") or "") in _SYSTEM_SUBTYPES:
            continue
        if drop_bots and (m.get("bot_id") or m.get("subtype")):
            continue
        if (
            not (m.get("text") or "").strip()
            and not (m.get("files") or [])
            and not (m.get("blocks") or [])
        ):
            continue
        out.append(m)
    return out


def _render_leaf(el: dict) -> str:
    """Render one Block Kit rich-text leaf element to text."""
    t = el.get("type")
    if t == "text":
        return el.get("text") or ""
    if t == "emoji":
        return f":{el.get('name', '')}:"
    if t == "link":
        url = el.get("url") or ""
        for scheme in ("mailto:", "tel:"):
            if url.startswith(scheme):
                url = url[len(scheme):]
                break
        return el.get("text") or url
    if t == "user":
        return f"@{el.get('user_id', '')}"
    if t == "usergroup":
        return f"@{el.get('usergroup_id', '')}"
    if t == "channel":
        return f"#{el.get('channel_id', '')}"
    if t == "broadcast":
        return f"@{el.get('range', '')}"
    return el.get("text") or ""


def _render_leaves(elements) -> str:
    return "".join(_render_leaf(x) for x in (elements or []))


def _render_rich_text_container(el: dict) -> str:
    """Render a rich_text container (section / list / quote / preformatted)."""
    t = el.get("type")
    if t == "rich_text_list":
        ordered = el.get("style") == "ordered"
        lines = []
        for i, item in enumerate(el.get("elements") or [], start=1):
            body = _render_leaves(item.get("elements"))
            if body:
                lines.append(f"{i}. {body}" if ordered else f"- {body}")
        return "\n".join(lines)
    body = _render_leaves(el.get("elements"))
    if t == "rich_text_quote":
        return "\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))
    return body  # rich_text_section, rich_text_preformatted


def _render_rich_text_block(block: dict) -> str:
    return "".join(
        _render_rich_text_container(el) for el in (block.get("elements") or []))


def _render_table_block(block: dict) -> str:
    """Render a Block Kit table as pipe-delimited rows (each cell is itself a
    rich_text block)."""
    lines = []
    for row in block.get("rows") or []:
        cells = [
            _render_rich_text_block(cell).strip().replace("\n", " ")
            for cell in row
        ]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _plain_text(obj) -> str:
    return obj.get("text") or "" if isinstance(obj, dict) else ""


def render_blocks(blocks: list[dict]) -> str:
    """Flatten Slack Block Kit *blocks* to plain text.

    Bot-posted reports (daily stats, compliance scans, …) carry their substance
    in ``rich_text`` and ``table`` blocks while the message ``text`` field holds
    only a short fallback summary. Rendering the blocks lets tabular report data
    survive into the text an agent reads. Unknown block types are skipped;
    returns ``""`` when nothing renders.
    """
    parts: list[str] = []
    for b in blocks or []:
        t = b.get("type")
        if t == "rich_text":
            parts.append(_render_rich_text_block(b))
        elif t == "table":
            parts.append(_render_table_block(b))
        elif t in ("section", "header"):
            parts.append(_plain_text(b.get("text")))
            for field in b.get("fields") or []:
                parts.append(_plain_text(field))
        elif t == "context":
            parts.append(" ".join(
                _plain_text(e) or _render_leaf(e)
                for e in (b.get("elements") or [])))
        # divider / image / actions / … carry no readable text — skip.
    rendered = "\n".join(p for p in parts if p and p.strip())
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()


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
        file_bits = " ".join(f"{name} {file_id}" for file_id, name in m.files)
        body = " ".join(part for part in (m.text, file_bits) if part)
        cost = _est_tokens(body) + _est_tokens(m.author) + 8
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


BACKFILL_HEADER = (
    "[recent channel history — snapshot; use fetch_channel_messages "
    "for more or newer messages]"
)
MESSAGES_HEADER = "[channel messages]"

_REL_SINCE = re.compile(r"^\s*(\d+)\s*([hd])\s*$", re.IGNORECASE)


def normalize_user(value: str | None) -> str:
    """Strip a Slack mention wrapper to a bare user id ('<@U063>' -> 'U063').

    Handles the ``<@ID>`` mention form (including the ``<@ID|handle>`` variant)
    and a leading ``@``; returns the bare id, or ``""`` when *value* is empty.
    """
    v = (value or "").strip()
    if v.startswith("<@") and v.endswith(">"):
        v = v[2:-1].split("|", 1)[0]
    elif v.startswith("@"):
        v = v[1:]
    return v.strip()


def parse_since(value: str | None, *, now: float) -> float | None:
    """Return an epoch cutoff for *value*, or None when empty.

    Accepts relative windows ('24h', '7d') evaluated against *now*, or an ISO
    date ('2026-07-01') meaning midnight UTC on that date. Raises ValueError on
    anything else so the caller can surface a 400 rather than broaden silently.
    """
    v = (value or "").strip()
    if not v:
        return None
    m = _REL_SINCE.match(v)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise ValueError(f"'since' window must be positive: {value!r}")
        unit = m.group(2).lower()
        return now - n * (3600 if unit == "h" else 86400)
    try:
        d = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Unparseable 'since' value: {value!r}") from exc
    return d.timestamp()


def _matches_user(message: RawMessage, query: str) -> bool:
    """Match a normalized user *query* against a message's Slack id or name.

    A ``U…`` id matches exactly (case-insensitive); a name query matches when it
    is a case-insensitive substring of the author's display name. The rendered
    channel block shows display names, not ids, so an agent filtering by the
    name it sees ("Flavius") must match — not only the raw id or ``<@U…>`` form.
    """
    if not query:
        return True
    q = query.lower()
    return message.author_id.lower() == q or q in (message.author or "").lower()


def filter_messages_for_query(
    messages: list[RawMessage], *, since_cutoff: float | None,
    user: str, limit: int,
) -> list[RawMessage]:
    """Filter newest-first *messages* by since/user, take the newest *limit*,
    and return them oldest-first for natural reading order."""
    out: list[RawMessage] = []
    for m in messages:  # newest-first
        if since_cutoff is not None and m.ts < since_cutoff:
            continue
        if not _matches_user(m, user):
            continue
        out.append(m)
        if len(out) >= max(1, limit):
            break
    return list(reversed(out))


def format_context_block(
    meta: ChannelMeta, messages: list[RawMessage], *, now: float,
    header: str = BACKFILL_HEADER,
) -> str | None:
    if not messages:
        return None
    lines = [header]
    lines.append(f"Channel: #{meta.name}" if meta.name else "Channel: (unnamed)")
    if meta.topic:
        lines.append(f"Topic: {meta.topic}")
    if meta.purpose:
        lines.append(f"Purpose: {meta.purpose}")
    lines.append("")
    lines.append("Recent messages (oldest to newest, bounded):")
    for m in messages:
        if m.text:
            lines.append(f"{_fmt_ts(m.ts)} {m.author}: {m.text}")
        for file_id, name in m.files:
            # A file-only message (empty text) attributes the file inline so it
            # never emits a blank "author: " line; file_id is sanitized like the
            # name so a crafted id can't forge extra context lines.
            prefix = "    " if m.text else f"{_fmt_ts(m.ts)} {m.author}: "
            lines.append(
                f"{prefix}shared file: {safe_display_name(name)} "
                f"(file: {safe_display_name(file_id)})"
            )
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
