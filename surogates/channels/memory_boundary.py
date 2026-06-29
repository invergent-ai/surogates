"""Conversation-privacy boundary tokens for agent memory isolation.

A channel agent's long-term memory is keyed by the token computed here so a
private conversation never shares memory with another.  Only a Slack *public*
channel collapses to the shared ``public`` token; every other surface — Slack
private channels and DMs, all Telegram chats, unknown platforms, blank ids —
gets an isolated token.  Fail closed: when in doubt, isolate.
"""

from __future__ import annotations

__all__ = ["MANAGED_CHANNELS", "boundary_token"]

# Channel platforms whose sessions are memory-partitioned by conversation.
MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "telegram"})


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
