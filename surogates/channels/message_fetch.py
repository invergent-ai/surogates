"""On-demand Slack channel message fetch — resolve creds, pull history, format.

The privileged half of the ``fetch_channel_messages`` tool: given a channel
session it resolves the bot token from the vault, calls the Slack
``fetch_channel_context`` primitive, and runs the pure query core. The bot token
never leaves the server. Missing configuration or credentials yield an empty
result with a human-readable ``note`` rather than an error; an unparseable
``since`` raises ``ValueError`` so the route can return a 400.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.channels.channel_backfill import (
    MESSAGES_HEADER,
    BackfillLimits,
    filter_messages_for_query,
    format_context_block,
    normalize_user,
    parse_since,
)
from surogates.channels.credentials import resolve_channel_credentials

_log = logging.getLogger(__name__)

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50
# When a since/user filter is set, the newest page alone can miss matches on a
# busy channel; scan a few bounded pages so the filter sees the real window.
# fetch_channel_context still honours its own per-fetch time budget.
_FILTER_MAX_PAGES = 5


def _clamp_limit(limit: Any) -> int:
    """Coerce a caller-supplied limit to 1..200; None/garbage/non-positive → default."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n < 1:
        return _DEFAULT_LIMIT
    return min(_MAX_LIMIT, n)


def _empty(note: str, channel: str = "") -> dict:
    return {"messages_block": None, "count": 0, "channel": channel, "note": note}


async def fetch_channel_messages(
    *, platform: Any, vault: Any, session: Any,
    limit: Any, since: str | None, user: str | None, now: float,
) -> dict:
    """Fetch recent messages for a Slack-channel session.

    Returns ``{"messages_block", "count", "channel", "note"}``. ``messages_block``
    is a formatted, oldest-first text block (or ``None`` when empty); ``note``
    explains why a result is empty. Raises ``ValueError`` on an unparseable
    ``since`` value.
    """
    cfg = getattr(session, "config", None) or {}
    identifier = cfg.get("channel_identifier") or ""
    channel_id = cfg.get("slack_channel_id") or ""
    if not identifier or not channel_id:
        return _empty("This session is not bound to a Slack channel.")

    since_cutoff = parse_since(since, now=now)  # may raise ValueError
    user_query = normalize_user(user)
    n = _clamp_limit(limit)

    try:
        refs = platform.descriptor.vault_refs(identifier)
        creds = await resolve_channel_credentials(
            vault=vault, kind="slack", identifier=identifier,
            org_id=str(session.org_id), refs=refs,
        )
        if not (creds or {}).get("bot_token"):
            return _empty("No Slack bot token is configured for this channel.")

        # A since/user filter needs more than the newest page to cover its
        # window; a plain "recent messages" call only needs one page.
        pages = _FILTER_MAX_PAGES if (user_query or since_cutoff is not None) else 1
        limits = BackfillLimits(max_pages=pages)
        result = await platform.fetch_channel_context(
            creds=creds, channel_id=channel_id, limits=limits, include_bots=True,
        )
    except Exception:
        _log.warning(
            "fetch_channel_messages failed for channel %s", channel_id,
            exc_info=True)
        return _empty("Could not read this channel's history right now.")

    if result is None:
        return _empty(
            "Could not read this channel's history (the bot may not be a "
            "member, or Slack returned an error).")

    meta, messages = result
    picked = filter_messages_for_query(
        messages, since_cutoff=since_cutoff, user=user_query, limit=n)
    if not picked:
        scope = " from that user" if user_query else ""
        return _empty(
            f"No messages found{scope} in the requested window.",
            channel=meta.name)
    block = format_context_block(meta, picked, now=now, header=MESSAGES_HEADER)
    return {
        "messages_block": block, "count": len(picked),
        "channel": meta.name, "note": None,
    }
