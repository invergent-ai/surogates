"""Redis handshake for the Slack 'Thinking…' placeholder.

Inbound stores a placeholder (the message id to later edit) keyed by session;
delivery reads it, edits that message into the reply, and clears the key. Lives
entirely in the channels process — no worker involvement.
"""
from __future__ import annotations

import json


def progress_key(kind: str, session_id) -> str:
    return f"channel-progress:{kind}:{session_id}"


async def set_placeholder(
    redis, kind: str, session_id, *, channel: str, ts: str, thread_ts, ttl_s: int = 600,
) -> None:
    await redis.set(
        progress_key(kind, session_id),
        json.dumps({"channel": channel, "ts": ts, "thread_ts": thread_ts}),
        ex=ttl_s,
    )


async def read_placeholder(redis, kind: str, session_id) -> dict | None:
    raw = await redis.get(progress_key(kind, session_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


async def clear_placeholder(redis, kind: str, session_id) -> None:
    await redis.delete(progress_key(kind, session_id))


async def take_progress_update(redis, kind: str, session_id, channel_id: str) -> str | None:
    """The placeholder ts to edit for this send, or None.

    Same-channel placeholder → its ts (left in Redis; caller clears after a
    successful send). Different-channel placeholder → stale: clear it, return
    None. No placeholder → None.
    """
    ph = await read_placeholder(redis, kind, session_id)
    if not ph:
        return None
    if ph.get("channel") != channel_id:
        await clear_placeholder(redis, kind, session_id)
        return None
    return ph.get("ts") or None
