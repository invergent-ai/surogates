"""Channel history backfill — pure core (filter, bound, format).

No I/O here: given raw platform messages + channel metadata + limits, produce
the single context block seeded into a channel session. Slack fetching, caching,
and session seeding live in the platform and coordinator layers.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone


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
