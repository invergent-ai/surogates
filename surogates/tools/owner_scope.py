"""Shared owner-scope predicate for operator-only tool behavior.

"Owner scope" means the calling session is the ops console (Studio)
live chat on a managed, non-system agent — the agent's operator, not
one of its end-users.  Tools use it to unlock cross-user data
(session_search widens to all principals; user_reports exists at all)
without ever trusting conversation content.  All conditions are
server-controlled:

- ``config.ops.user_id`` must be stamped (``create_live_session`` in
  surogate-ops).  The public session API copies client config
  verbatim, so this alone is forgeable — hence the next check.  The
  same stamp is parsed in ``orchestrator/mcp_client.py`` and
  ``tools/mcp/client.py``; keep them in sync.
- the session principal must be a service account whose name carries
  the ops live-chat prefix.  End-user surfaces (web SPA, website
  widget, managed channels) never run under a service account, and an
  org API token's own service account cannot match the prefix unless
  an org admin deliberately named it so.
- ``config.agent_type`` must be absent: surogate-ops always stamps it
  for system agents, and their sessions must stay private per user.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Matches ops_chat_service_account_name in surogate-ops
# (core/ops_chat.py): "ops-chat-{org_id}-{user_id}".  Only
# surogate-ops's live-chat forwarder authenticates with these
# accounts; creating one with a matching name requires org-admin
# rights.
OPS_CHAT_SA_PREFIX = "ops-chat-"


def owner_scope_config_ok(
    service_account_id: UUID | None, session_config: Any,
) -> bool:
    """The cheap, DB-free subset of the owner-scope conditions.

    Used where a database round-trip is unwelcome (per-session tool
    visibility); NOT sufficient on its own — handlers must confirm
    with :func:`is_owner_scoped` before returning cross-user data.
    """
    if service_account_id is None:
        return False
    if not isinstance(session_config, dict):
        return False
    ops = session_config.get("ops")
    if not (isinstance(ops, dict) and ops.get("user_id")):
        return False
    return not session_config.get("agent_type")


async def is_owner_scoped(
    session_store: Any,
    service_account_id: UUID | None,
    session_config: Any,
) -> bool:
    """Full owner-scope check, including the service-account name proof."""
    if not owner_scope_config_ok(service_account_id, session_config):
        return False
    try:
        from sqlalchemy import text as sa_text

        async with session_store._sf() as db:
            row = await db.execute(
                sa_text("SELECT name FROM service_accounts WHERE id = :id"),
                {"id": service_account_id},
            )
            name = row.scalar_one_or_none()
    except Exception:
        logger.warning(
            "Owner-scope service-account lookup failed; treating as "
            "principal-scoped",
            exc_info=True,
        )
        return False
    return bool(name) and name.startswith(OPS_CHAT_SA_PREFIX)
