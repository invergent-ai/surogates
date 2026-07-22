"""Enrollment, visibility, and removal for the ``agent_users`` table.

One module so every writer — session creation, the web-channel login
paths, and the ops management client — shares the same idempotent
semantics instead of hand-rolled variants.

Removal is a **tombstone** (``removed_at``), never a row delete. The
tombstone is what makes three behaviors hold at once: the migrate-time
backfill cannot resurrect a removed binding (the row still exists, so
ON CONFLICT skips it), an automatic re-enroll at login/session cannot
undo an operator's removal (same reason), and no destructive account
delete is needed for users with history. Re-adding is explicit: the
manual attach path reactivates the tombstoned row.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from sqlalchemy import select, text
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

# Channels whose sessions represent a real end-user talking to the
# agent. Everything else (api, task, delegation, worker, scheduled,
# ambient, browser_setup — which even uses the phantom agent id
# "browser-setup") must never mint a binding. Mirrors the ops-side
# USER_STATS_INTERACTIVE_CHANNELS so the roster and its stats agree.
ENROLLMENT_CHANNELS = frozenset({"web", "website", "slack", "telegram"})

# Idempotent backfill from historical sessions — safe to run on every
# migrate. Interactive channels only (same set as ENROLLMENT_CHANNELS);
# ON CONFLICT also guarantees a tombstoned (operator-removed) binding
# is never resurrected by a re-run.
BACKFILL_SQL = """
INSERT INTO agent_users (id, org_id, agent_id, user_id, source, created_at)
SELECT gen_random_uuid(), s.org_id, s.agent_id, s.user_id,
       'backfill', MIN(s.created_at)
FROM sessions s
WHERE s.user_id IS NOT NULL
  AND s.agent_id IS NOT NULL
  AND s.agent_id <> ''
  AND s.channel IN ('web', 'website', 'slack', 'telegram')
GROUP BY s.org_id, s.agent_id, s.user_id
ON CONFLICT (org_id, agent_id, user_id) DO NOTHING
"""


def visible_users_filter(org_id: UUID, agent_id: str):
    """Filter for ``select(User)``: one agent's Configure → Users view.

    The agent's actively bound users, plus org users with no binding
    row AT ALL — unbound accounts (legacy rows) must stay visible
    somewhere or they become unmanageable orphans. A tombstoned
    (removed) binding is deliberately neither: the user neither shows
    on this agent nor resurfaces everywhere as an orphan — re-adding
    them is the explicit attach path.
    """
    from sqlalchemy import or_

    from surogates.db.models import User

    bound_here = select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
        AgentUser.agent_id == agent_id,
        AgentUser.removed_at.is_(None),
    )
    any_binding_row = select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
    )
    return or_(User.id.in_(bound_here), User.id.not_in(any_binding_row))


async def user_visibility(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
) -> str | None:
    """How one agent's Configure → Users tab may act on a user.

    ``"bound"`` — actively enrolled on this agent; ``"orphan"`` —
    exists in the org with no binding row at all (managed from any
    tab); ``None`` — nonexistent, another agent's user, or removed
    from this agent (re-add via the attach path, not edit-in-place).
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
                select(AgentUser.agent_id, AgentUser.removed_at).where(
                    AgentUser.org_id == org_id,
                    AgentUser.user_id == user_id,
                )
            )
        )
        .all()
    )
    if any(a == agent_id and removed is None for a, removed in bindings):
        return "bound"
    return "orphan" if not bindings else None


async def purge_user_account(db: AsyncSession, *, org_id: UUID, user_id: UUID) -> None:
    """Hard-delete an account, detaching its history first.

    Explicit raw-SQL cleanup (mirroring ops' ``delete_agent_data``
    discipline) because the ORM relationships are ``lazy="raise"`` and
    the ``user_id`` FKs carry no ``ondelete`` — a bare ORM delete
    fails at flush for any user with sessions or channel identities.
    Sessions/events/audit rows survive with ``user_id`` detached (they
    are org history, not user property); identity rows and the user's
    own inbox go with the account. Caller commits.
    """
    params = {"uid": user_id, "org": org_id}
    for stmt in (
        "DELETE FROM channel_identities WHERE user_id = :uid AND org_id = :org",
        "DELETE FROM inbox_items WHERE user_id = :uid AND org_id = :org",
        "DELETE FROM agent_users WHERE user_id = :uid AND org_id = :org",
        "UPDATE sessions SET user_id = NULL WHERE user_id = :uid AND org_id = :org",
        "UPDATE events SET user_id = NULL WHERE user_id = :uid AND org_id = :org",
        "UPDATE audit_log SET user_id = NULL WHERE user_id = :uid AND org_id = :org",
        "UPDATE missions SET user_id = NULL WHERE user_id = :uid AND org_id = :org",
        "DELETE FROM users WHERE id = :uid AND org_id = :org",
    ):
        await db.execute(text(stmt), params)


async def remove_user_from_agent(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
) -> bool:
    """Remove a user from one agent's Configure → Users tab.

    Actively bound here: tombstone the binding (``removed_at``) — the
    account and its history survive, other agents' bindings are
    untouched, and neither the backfill nor an automatic login/session
    re-enroll can bring the user back (ON CONFLICT hits the surviving
    row). Orphan (no binding rows at all): hard account delete via
    :func:`purge_user_account`. Returns False when the user isn't
    visible from this tab. Stages writes only — the caller commits.
    """
    visibility = await user_visibility(
        db, org_id=org_id, agent_id=agent_id, user_id=user_id,
    )
    if visibility == "bound":
        await db.execute(
            text(
                "UPDATE agent_users SET removed_at = now() "
                "WHERE org_id = :org AND agent_id = :agent "
                "AND user_id = :uid AND removed_at IS NULL"
            ),
            {"org": org_id, "agent": agent_id, "uid": user_id},
        )
        return True
    if visibility == "orphan":
        await purge_user_account(db, org_id=org_id, user_id=user_id)
        return True
    return False


async def ensure_agent_user(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
    source: str,
    reactivate: bool = False,
) -> None:
    """Record that *user_id* belongs to *agent_id* (idempotent).

    First write wins — an existing binding keeps its original source
    and timestamp, and a tombstoned binding stays removed, so the
    automatic writers (login, first session, backfill) can never undo
    an operator's removal. ``reactivate=True`` is the explicit re-add
    (the manual attach path): it clears the tombstone. Callers commit;
    this only stages the write so it rides in their transaction.
    """
    stmt = pg_insert(AgentUser).values(
        id=uuid.uuid4(),
        org_id=org_id,
        agent_id=agent_id,
        user_id=user_id,
        source=source,
    )
    if reactivate:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_users_org_agent_user",
            set_={"removed_at": None, "source": source},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_agent_users_org_agent_user",
        )
    await db.execute(stmt)
