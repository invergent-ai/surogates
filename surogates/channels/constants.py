"""Channel capability constants shared across process boundaries.

The session store (worker process) must know facts about the channel
adapters (channels process) without importing the httpx-heavy platform
modules: which channels have a delivery loop claiming their outbox rows,
and which can render an interactive ``ask_user_question`` prompt.  Keeping
both sets here — next to the platform package that must stay in lockstep
with them — gives registration and consumption one shared source instead
of literals scattered per file.
"""

from __future__ import annotations

__all__ = [
    "ADAPTER_CHANNELS",
    "END_USER_CHANNELS",
    "INBOX_NOTIFY_CHANNELS",
    "INTERACTIVE_PROMPT_CHANNELS",
    "multi_session_disabled",
]


def multi_session_disabled(config: dict) -> bool:
    """Whether a routing/channel config turns the "multi session" capability off.

    Only an explicit ``False`` counts — an absent key means the capability
    is on (the default), so this must never be "simplified" to
    ``not config.get(...)``.
    """
    return config.get("multi_session") is False

#: Channels with a registered outbound delivery adapter (a delivery loop
#: that claims their outbox rows).  Must match the platforms registered in
#: :mod:`surogates.channels.platforms` — an outbox row for any other
#: channel value would never be claimed and sit "pending" forever.
ADAPTER_CHANNELS = frozenset({"slack", "telegram", "whatsapp"})

#: Channels whose platform can render an ``ask_user_question`` prompt
#: (Slack: Answer button + modal; Telegram: inline keyboard; WhatsApp:
#: numbered plain text).  A channel OUTSIDE this set gets no
#: ``input_prompt`` outbox row at all — ``_build_channel_payload`` returns
#: an empty payload and ``store`` drops it — so the user sees nothing and
#: the session parks waiting for an answer that can never arrive.  Adding a
#: channel here therefore REQUIRES a prompt renderer in its ``send``.
INTERACTIVE_PROMPT_CHANNELS = frozenset({"slack", "telegram", "whatsapp"})

#: Channels whose sessions represent a real end-user talking to the
#: agent — the set that drives agent-user enrollment (``agent_users``)
#: and, via the ops control plane, the Users-page activity stats.
#: Everything else (api, task, delegation, worker, scheduled, ambient,
#: browser_setup) must never mint a binding or count as end-user
#: activity.  ``teams`` is pre-registered for the Phase-2 adapter
#: (``channels/teams.py`` already pins ``channel='teams'``) so shipping
#: it cannot silently split the roster from its stats.
END_USER_CHANNELS = frozenset(
    {"web", "website", "slack", "telegram", "teams", "whatsapp"}
)

#: Channels whose completed sessions raise a ``task_complete`` inbox item.
#: Narrower than :data:`END_USER_CHANNELS` on purpose — this set is about
#: where the notification can be RETIRED, not who is talking.  The item is
#: cleared by opening its session's conversation (the chat surfaces delete
#: it on open, and the store suppresses it outright for a live viewer), so
#: a channel outside this set mints rows nothing can ever clear: ``api``,
#: ``worker``/``task``/``delegation``, ``scheduled``, ``ambient`` and
#: ``browser_setup`` have no conversation a person opens, and the adapter
#: channels deliver the result in the conversation itself, where a second
#: "Task complete" is noise.  Child sessions are excluded regardless of
#: channel — see :func:`surogates.session.inbox_payload.raises_completion_inbox_item`.
INBOX_NOTIFY_CHANNELS = frozenset({"web", "website"})
