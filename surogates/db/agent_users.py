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

import uuid
from uuid import UUID

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from surogates.channels.constants import END_USER_CHANNELS
from surogates.db.models import (
    AgentUser,
    AuditLog,
    ChannelIdentity,
    Event,
    InboxItem,
    Mission,
    Session,
    User,
)

# Where a binding came from. Kept as plain strings (not an enum) so a
# future writer can add a source without a schema migration.
SOURCE_MANUAL = "manual"
SOURCE_LOGIN = "login"
SOURCE_SESSION = "session"
SOURCE_BACKFILL = "backfill"

# The channels that enroll users on agents — the canonical set lives in
# channels/constants.py next to the adapter registry it must track.
ENROLLMENT_CHANNELS = END_USER_CHANNELS

# Idempotent backfill from historical sessions — safe to run on every
# migrate. End-user channels only, generated from the same frozenset
# the live enrollment uses so the two can never drift; ON CONFLICT
# also guarantees a tombstoned (operator-removed) binding is never
# resurrected by a re-run.
BACKFILL_SQL = f"""
INSERT INTO agent_users (id, org_id, agent_id, user_id, source, created_at)
SELECT gen_random_uuid(), s.org_id, s.agent_id, s.user_id,
       '{SOURCE_BACKFILL}', MIN(s.created_at)
FROM sessions s
WHERE s.user_id IS NOT NULL
  AND s.agent_id IS NOT NULL
  AND s.agent_id <> ''
  AND s.channel IN ({", ".join(sorted(repr(c) for c in END_USER_CHANNELS))})
GROUP BY s.org_id, s.agent_id, s.user_id
ON CONFLICT (org_id, agent_id, user_id) DO NOTHING
"""


def active_bound_user_ids(org_id: UUID, agent_ids: list[str]):
    """Select of user ids actively bound (not tombstoned) to the agents.

    The one definition of "this agent's users" — the Configure list,
    the Users-page roster, and the assigned-flag reads all build on it
    so removal semantics can never drift between surfaces.
    """
    return select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
        AgentUser.agent_id.in_(agent_ids),
        AgentUser.removed_at.is_(None),
    )


def visible_users_filter(org_id: UUID, agent_id: str):
    """Filter for ``select(User)``: one agent's Configure → Users view.

    The agent's actively bound users, plus org users with no binding
    row AT ALL — unbound accounts (legacy rows) must stay visible
    somewhere or they become unmanageable orphans. A tombstoned
    (removed) binding is deliberately neither: the user neither shows
    on this agent nor resurfaces everywhere as an orphan — re-adding
    them is the explicit attach path.
    """
    any_binding_row = select(AgentUser.user_id).where(
        AgentUser.org_id == org_id,
    )
    return or_(
        User.id.in_(active_bound_user_ids(org_id, [agent_id])),
        User.id.not_in(any_binding_row),
    )


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
    One round trip: three EXISTS in a single SELECT.
    """
    user_rows = select(User.id).where(User.id == user_id, User.org_id == org_id)
    any_binding = select(AgentUser.id).where(
        AgentUser.org_id == org_id, AgentUser.user_id == user_id,
    )
    bound_here = any_binding.where(
        AgentUser.agent_id == agent_id, AgentUser.removed_at.is_(None),
    )
    user_exists, has_bindings, is_bound = (
        await db.execute(
            select(exists(user_rows), exists(any_binding), exists(bound_here))
        )
    ).one()
    if not user_exists:
        return None
    if is_bound:
        return "bound"
    return "orphan" if not has_bindings else None


async def purge_user_account(db: AsyncSession, *, org_id: UUID, user_id: UUID) -> None:
    """Hard-delete an account, detaching its history first.

    Explicit statement-per-table cleanup (mirroring ops'
    ``delete_agent_data`` discipline) because the ORM relationships
    are ``lazy="raise"`` and the ``user_id`` FKs carry no ``ondelete``
    — a bare ORM instance delete fails at flush for any user with
    sessions or channel identities. Sessions/events/audit rows survive
    with ``user_id`` detached (they are org history, not user
    property); identity rows and the user's own inbox go with the
    account. The FK-coverage test in the integration suite pins this
    list against the schema. Caller commits.
    """
    def scoped(model):
        return (model.user_id == user_id, model.org_id == org_id)

    for stmt in (
        delete(ChannelIdentity).where(*scoped(ChannelIdentity)),
        delete(InboxItem).where(*scoped(InboxItem)),
        delete(AgentUser).where(*scoped(AgentUser)),
        update(Session).where(*scoped(Session)).values(user_id=None),
        update(Event).where(*scoped(Event)).values(user_id=None),
        update(AuditLog).where(*scoped(AuditLog)).values(user_id=None),
        update(Mission).where(*scoped(Mission)).values(user_id=None),
        delete(User).where(User.id == user_id, User.org_id == org_id),
    ):
        await db.execute(stmt)


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
    # Tombstone-first: the common case is one UPDATE, no pre-reads.
    result = await db.execute(
        update(AgentUser)
        .where(
            AgentUser.org_id == org_id,
            AgentUser.agent_id == agent_id,
            AgentUser.user_id == user_id,
            AgentUser.removed_at.is_(None),
        )
        .values(removed_at=func.now())
    )
    if result.rowcount:
        return True
    if (
        await user_visibility(
            db, org_id=org_id, agent_id=agent_id, user_id=user_id,
        )
        == "orphan"
    ):
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
