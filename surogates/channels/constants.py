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
ADAPTER_CHANNELS = frozenset({"slack", "telegram"})

#: Channels whose platform can render an ``ask_user_question`` prompt
#: (Slack: Answer button + modal; Telegram: inline keyboard).  Channels
#: outside this set must not receive content-less ``input_prompt`` outbox
#: rows — their ``send`` would post an empty message.
INTERACTIVE_PROMPT_CHANNELS = frozenset({"slack", "telegram"})

#: Channels whose sessions represent a real end-user talking to the
#: agent — the set that drives agent-user enrollment (``agent_users``)
#: and, via the ops control plane, the Users-page activity stats.
#: Everything else (api, task, delegation, worker, scheduled, ambient,
#: browser_setup) must never mint a binding or count as end-user
#: activity.  ``teams`` is pre-registered for the Phase-2 adapter
#: (``channels/teams.py`` already pins ``channel='teams'``) so shipping
#: it cannot silently split the roster from its stats.
END_USER_CHANNELS = frozenset({"web", "website", "slack", "telegram", "teams"})
