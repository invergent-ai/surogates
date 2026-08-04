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
    "API_CHANNEL",
    "DIRECT_UI_CHANNELS",
    "END_USER_CHANNELS",
    "INBOX_NOTIFY_CHANNELS",
    "INTERACTIVE_PROMPT_CHANNELS",
    "SERVICE_ACCOUNT_CHANNELS",
    "STUDIO_CHANNEL",
    "multi_session_disabled",
]

#: A programmatic client driving the agent with a service-account token.
API_CHANNEL = "api"

#: An operator talking to their own agent from the Surogate Studio Work
#: UI.  Authenticated exactly like :data:`API_CHANNEL` (the control plane
#: forwards with an ``ops-chat-*`` service-account token), so everything
#: that reasons about *authentication* must treat the two alike — see
#: :data:`SERVICE_ACCOUNT_CHANNELS`.  It is a separate channel value
#: because it answers a different question: ``api`` means a third party
#: integrated against the API, ``studio`` means the operator themself.
#: Reporting the two as one channel made every operator chat look like a
#: third-party integration.
STUDIO_CHANNEL = "studio"


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
#: Everything else (api, studio, task, delegation, worker, scheduled,
#: ambient, browser_setup) must never mint a binding or count as
#: end-user activity — ``studio`` in particular is the operator talking
#: to their own agent, not a customer of it.  ``teams`` is pre-registered for the Phase-2 adapter
#: (``channels/teams.py`` already pins ``channel='teams'``) so shipping
#: it cannot silently split the roster from its stats.
END_USER_CHANNELS = frozenset(
    {"web", "website", "slack", "telegram", "teams", "whatsapp"}
)

#: Channels created by a service-account token rather than a logged-in
#: end-user, so the session row carries ``service_account_id`` and no
#: ``user_id``.  Governs decisions that turn on *how the caller
#: authenticated*: the prompt-injection detector's source profile and
#: the absence of a per-user storage scope.
SERVICE_ACCOUNT_CHANNELS = frozenset({API_CHANNEL, STUDIO_CHANNEL})

#: Channels whose client renders the conversation itself, live, as events
#: land — as opposed to a messaging platform that only ever sees what an
#: outbound adapter delivers to it.  Governs two behaviours that would
#: otherwise silently regress when a channel is added:
#:
#: * long code runs post a throttled "still working" heartbeat to channel
#:   sessions only, because these clients already stream progress; and
#: * a scheduled run delivers its result back to its parent conversation
#:   only when that parent is one of these — the messaging platforms get
#:   the result through their own adapter instead.
#:
#: A channel missing from this set therefore loses scheduled-run results
#: entirely, which is why the membership lives here rather than as a
#: literal at each call site.
DIRECT_UI_CHANNELS = frozenset({"web", API_CHANNEL, STUDIO_CHANNEL})

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
