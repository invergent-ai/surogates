"""Enrollment writes for the ``agent_users`` binding table.

One tiny module so every writer — session creation, the web-channel
login paths, and the ops management client — shares the same
idempotent INSERT instead of three hand-rolled variants.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import AgentUser

logger = logging.getLogger(__name__)

# Where a binding came from. Kept as plain strings (not an enum) so a
# future writer can add a source without a schema migration.
SOURCE_MANUAL = "manual"
SOURCE_LOGIN = "login"
SOURCE_SESSION = "session"
SOURCE_BACKFILL = "backfill"

# Idempotent backfill from historical sessions — safe to run on every
# migrate. Interactive channels only: api/service-account traffic has
# no end-user, and subagent rows inherit the parent's attribution.
BACKFILL_SQL = """
INSERT INTO agent_users (id, org_id, agent_id, user_id, source, created_at)
SELECT gen_random_uuid(), s.org_id, s.agent_id, s.user_id,
       'backfill', MIN(s.created_at)
FROM sessions s
WHERE s.user_id IS NOT NULL
  AND s.agent_id IS NOT NULL
  AND s.agent_id <> ''
GROUP BY s.org_id, s.agent_id, s.user_id
ON CONFLICT (org_id, agent_id, user_id) DO NOTHING
"""


def visible_users_filter(org_id: UUID, agent_id: str):
    """Filter for ``select(User)``: one agent's Configure → Users view.

    The agent's bound users, plus org users bound to NO agent at all —
    unbound accounts (legacy rows, pre-provisioned users whose agent
    was deleted) must stay visible somewhere or they become
    unmanageable orphans.
    """
    from sqlalchemy import or_

    from surogates.db.models import User

    bound_here = select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
        AgentUser.agent_id == agent_id,
    )
    bound_anywhere = select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
    )
    return or_(User.id.in_(bound_here), User.id.not_in(bound_anywhere))


async def user_visibility(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
) -> str | None:
    """How one agent's Configure → Users tab may act on a user.

    ``"bound"`` — enrolled on this agent; ``"orphan"`` — exists in the
    org but bound to no agent at all (managed from any tab); ``None``
    — nonexistent here, or another agent's user.
    """
    from surogates.db.models import User

    user = (
        await db.execute(
            select(User.id).where(User.id == user_id, User.org_id == org_id)
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    bindings = (
        (
            await db.execute(
                select(AgentUser.agent_id).where(
                    AgentUser.org_id == org_id,
                    AgentUser.user_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if agent_id in bindings:
        return "bound"
    return "orphan" if not bindings else None


async def remove_user_from_agent(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
) -> bool:
    """Remove a user from one agent's Configure → Users tab.

    Bound user: drop this agent's binding; the ACCOUNT is deleted only
    when no other agent still has the user — a shared account must
    survive removal from one of its agents.  Orphan (no bindings
    anywhere): plain account delete.  Returns False when the user
    isn't visible from this tab.  Stages deletes only — the caller
    commits.
    """
    from surogates.db.models import User

    user = (
        await db.execute(
            select(User).where(User.id == user_id, User.org_id == org_id)
        )
    ).scalar_one_or_none()
    if user is None:
        return False
    bindings = (
        (
            await db.execute(
                select(AgentUser).where(
                    AgentUser.org_id == org_id,
                    AgentUser.user_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    here = [b for b in bindings if b.agent_id == agent_id]
    if bindings and not here:
        return False
    for binding in here:
        await db.delete(binding)
    if len(bindings) == len(here):
        # Last (or only) home — remove the account itself.
        await db.delete(user)
    return True


async def ensure_agent_user(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
    source: str,
) -> None:
    """Record that *user_id* belongs to *agent_id* (idempotent).

    First write wins — an existing binding keeps its original source
    and timestamp. Callers commit; this only stages the INSERT so it
    rides in the caller's transaction.
    """
    await db.execute(
        pg_insert(AgentUser)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            agent_id=agent_id,
            user_id=user_id,
            source=source,
        )
        .on_conflict_do_nothing(
            constraint="uq_agent_users_org_agent_user",
        )
    )
