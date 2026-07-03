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


def code_run_key(kind: str, session_id, run_id: str) -> str:
    return f"channel-coderun:{kind}:{session_id}:{run_id}"


async def read_code_run_ts(
    redis, kind: str, session_id, run_id: str, channel_id: str,
) -> str | None:
    """The ts of this run's 'main coding message' to edit, or None.

    Returns the recorded ts only when it was posted to the *same* channel (a
    different channel means the run's message lives elsewhere — post fresh).
    None when no message has been posted for this run yet (the first heartbeat
    posts it) or the record is malformed.
    """
    raw = await redis.get(code_run_key(kind, session_id, run_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("channel") != channel_id:
        return None
    return obj.get("ts") or None


async def set_code_run_ts(
    redis, kind: str, session_id, run_id: str, *, channel: str, ts: str,
    ttl_s: int = 1800,
) -> None:
    """Record (and refresh the TTL of) this run's main-message ts for editing."""
    await redis.set(
        code_run_key(kind, session_id, run_id),
        json.dumps({"channel": channel, "ts": ts}),
        ex=ttl_s,
    )


async def post_placeholder_once(
    redis, kind: str, session_id, *, post, channel: str, thread_ts,
) -> str | None:
    """Post a placeholder and record it, unless one is already pending.

    ``post`` is a zero-arg async callable that posts the placeholder and returns
    its message ts (or None). If a placeholder already exists for this session —
    a rapid follow-up message before the worker has replied — we do NOT post a
    second one: the existing placeholder is left to be edited into the reply, so
    the first is never orphaned. Returns the new ts, or None when skipped or the
    post failed.
    """
    if await read_placeholder(redis, kind, session_id) is not None:
        return None
    ts = await post()
    if ts:
        await set_placeholder(
            redis, kind, session_id, channel=channel, ts=ts, thread_ts=thread_ts,
        )
    return ts
