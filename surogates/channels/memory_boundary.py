"""Conversation-privacy boundary tokens for agent memory isolation.

A channel agent's long-term memory is keyed by the token computed here so a
private conversation never shares memory with another.  Only a Slack *public*
channel collapses to the shared ``public`` token; every other surface — Slack
private channels and DMs, all Telegram chats, unknown platforms, blank ids —
gets an isolated token.  Fail closed: when in doubt, isolate.
"""

from __future__ import annotations

__all__ = [
    "MANAGED_CHANNELS",
    "EVAL_BOUNDARY_PREFIX",
    "boundary_token",
    "is_eval_session",
    "session_memory_boundary",
]

# Channel platforms whose sessions are memory-partitioned by conversation.
MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "telegram", "whatsapp"})

# Memory-boundary namespace for evaluation sessions. Outside the managed
# channels this is the ONLY prefix honoured: an evaluation needs a scratch
# partition, and letting a caller name any boundary would let it read or
# overwrite the memory of a private conversation on the same agent.
EVAL_BOUNDARY_PREFIX = "eval:"


def boundary_token(
    *,
    platform: str,
    channel_id: str,
    visibility: str,
    source: dict,
    fallback_id: str,
) -> str:
    """Return the memory-boundary token for one inbound conversation.

    ``visibility`` is the coarse privacy (``public`` | ``private`` | ``dm``);
    ``source`` carries raw adapter metadata (``chat_type`` for Telegram);
    ``fallback_id`` is a deterministic isolated id used when ``channel_id`` is
    blank (so two unknown conversations never collide).
    """
    cid = (channel_id or "").strip()

    if platform == "slack":
        if not cid:
            return f"slack:iso:{fallback_id}"
        if visibility == "public":
            return "public"
        if visibility == "dm":
            return f"slack:d:{cid}"
        # private channel, or anything unrecognized → isolated (fail closed)
        return f"slack:c:{cid}"

    if platform == "telegram":
        if not cid:
            return f"telegram:iso:{fallback_id}"
        chat_type = str((source or {}).get("chat_type", ""))
        if visibility == "dm" or chat_type == "private":
            return f"tg:d:{cid}"
        if chat_type == "channel":
            return f"tg:c:{cid}"
        if chat_type in ("group", "supergroup"):
            return f"tg:g:{cid}"
        return f"telegram:iso:{fallback_id}"  # unknown telegram surface → isolated

    # Unknown platform → fail closed, isolated.
    return f"{platform}:iso:{fallback_id}"


def is_eval_session(session: object) -> bool:
    """True when this session is one row of an evaluation run.

    Keyed on the server-stamped ``memory_boundary``, which is unforgeable:
    ``apply_eval_isolation`` strips any client-supplied boundary before
    deciding whether to stamp its own.  This prevents a client from
    supplying ``eval_run_id`` in config and claiming evaluation treatment
    for a session the server never isolated.
    """
    cfg = getattr(session, "config", None) or {}
    boundary = str(cfg.get("memory_boundary") or "").strip()
    return boundary.startswith(EVAL_BOUNDARY_PREFIX)


def _legacy_boundary_fallback_id(session: object, cfg: dict) -> str:
    return str(cfg.get("channel_session_key") or getattr(session, "id", ""))


def session_memory_boundary(session: object) -> str | None:
    """Memory boundary for a session, or ``None`` to keep the per-user layout.

    Managed-channel sessions key memory per conversation: the persisted
    ``config["memory_boundary"]`` when present, otherwise a fail-closed legacy
    boundary.  Only older Slack rows with a confident public channel id
    (``C...``) collapse to ``public``; every other older row is isolated by
    ``channel_session_key`` or ``session.id``.  Every non-channel session
    returns ``None`` so the caller keeps today's per-user / shared memory,
    except an evaluation session, which carries an ``eval:`` boundary stamped
    by the session route.
    """
    channel = getattr(session, "channel", None)
    cfg = getattr(session, "config", None) or {}
    persisted = str(cfg.get("memory_boundary") or "").strip()
    if channel not in MANAGED_CHANNELS:
        if persisted.startswith(EVAL_BOUNDARY_PREFIX):
            return persisted
        return None
    if persisted:
        return persisted

    fallback_id = _legacy_boundary_fallback_id(session, cfg)
    if channel == "slack":
        slack_channel_id = str(cfg.get("slack_channel_id") or "").strip()
        if slack_channel_id.startswith("C"):
            return "public"
        return f"slack:iso:{fallback_id}"
    if channel == "telegram":
        return f"telegram:iso:{fallback_id}"
    return f"{channel}:iso:{fallback_id}"
