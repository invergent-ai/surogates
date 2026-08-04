"""Relabel historical operator conversations onto the ``studio`` channel.

Every Studio Work chat used to be stamped ``channel='api'``, because the
control plane creates it through the service-account route and that route
hardcoded :data:`~surogates.channels.constants.API_CHANNEL`.  The label was
wrong in a way that showed: ``api`` is meant to identify a third party
integrated against the API, so an operator's own conversation with their
own agent was reported as an external integration everywhere the channel
column is read — the sessions explorer's channel filter, the agent
overview's channel breakdown, and any per-channel analytics built on them.

Callers now declare :data:`~surogates.channels.constants.STUDIO_CHANNEL`
at creation, which fixes new rows.  This backfill fixes the existing ones.

Idempotent by construction: it only ever matches rows still carrying the
old label, so a second run is a no-op rather than a fight with live data.
The ``ops-chat-`` account name is the discriminator because it is the only
thing that distinguishes the two — both authenticate identically, and only
the control plane's live-chat forwarder holds one of these accounts.
"""

from __future__ import annotations

from surogates.channels.constants import API_CHANNEL, STUDIO_CHANNEL
from surogates.tools.owner_scope import OPS_CHAT_SA_PREFIX

__all__ = ["RELABEL_STUDIO_SESSIONS_SQL"]

# Restricted to the old label so re-runs match nothing, and joined against
# service_accounts rather than trusting the session row alone: a session
# with no service account (an end-user's web chat) must never be swept up.
RELABEL_STUDIO_SESSIONS_SQL = f"""
UPDATE sessions AS s
SET channel = '{STUDIO_CHANNEL}'
FROM service_accounts AS sa
WHERE sa.id = s.service_account_id
  AND s.channel = '{API_CHANNEL}'
  AND sa.name LIKE '{OPS_CHAT_SA_PREFIX}%'
"""
