"""Who belongs to an organisation: its home users, and its members.

Every user has a home organisation, ``users.org_id``, which holds their
account. ``org_members`` lists the further organisations a user belongs
to. A token signed in through any of them is accepted while its user
belongs to that organisation, so one login serves them all.

:func:`belongs_to` is the one SQL condition for "a user of this
organisation", home or member. The writers here stage their changes
only: the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from surogates.db.models import Org, OrgMember, User

__all__ = [
    "MembershipError",
    "MembershipErrorCode",
    "MembershipsRemain",
    "OrgRef",
    "add_member",
    "belongs_to",
    "remove_member",
    "user_orgs",
]

MembershipErrorCode = Literal["unknown-org", "unknown-user", "email-taken", "home-org"]


class MembershipError(Exception):
    """A membership change refused, for the reason its ``code`` names."""

    def __init__(self, code: MembershipErrorCode) -> None:
        super().__init__(code)
        self.code: MembershipErrorCode = code


@dataclass(frozen=True)
class OrgRef:
    id: UUID
    name: str


class MembershipsRemain(Exception):
    """A home account is not deleted while it still belongs to other organisations.

    Deleting it would take the user out of those organisations too, and
    their rows there name the user. Remove those memberships first.
    """

    def __init__(self, orgs: list[OrgRef]) -> None:
        self.orgs = orgs
        named = ", ".join(f"{o.name} ({o.id})" for o in orgs)
        super().__init__(
            f"This account still belongs to other organisations: {named}. "
            "Remove it from them first, then delete it."
        )


def belongs_to(org_id: UUID) -> ColumnElement[bool]:
    """``User`` rows of *org_id*: its home users and its members."""
    return or_(
        User.org_id == org_id,
        User.id.in_(select(OrgMember.user_id).where(OrgMember.org_id == org_id)),
    )


async def user_orgs(session: AsyncSession, user_id: UUID) -> list[OrgRef]:
    """Every organisation *user_id* belongs to: the home one first, then the others by name, then by id."""
    home = select(User.org_id).where(User.id == user_id).scalar_subquery()
    is_home = Org.id == home
    rows = await session.execute(
        select(Org.id, Org.name)
        .where(or_(is_home, Org.id.in_(select(OrgMember.org_id).where(OrgMember.user_id == user_id))))
        .order_by(is_home.desc(), Org.name, Org.id)
    )
    return [OrgRef(id=row.id, name=row.name) for row in rows]


async def add_member(session: AsyncSession, org_id: UUID, user_id: UUID) -> bool:
    """Make *user_id* a member of *org_id*; False when they already belong to it.

    The organisation's row is locked first, so two adds to one
    organisation run one after the other and the email check below
    sees the other's member. A user whose email (case-insensitive)
    another user of the organisation has is refused: the email is the
    password login's key there.
    """
    locked = await session.scalar(select(Org.id).where(Org.id == org_id).with_for_update())
    if locked is None:
        raise MembershipError("unknown-org")
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise MembershipError("unknown-user")
    already = await session.scalar(select(User.id).where(User.id == user_id, belongs_to(org_id)))
    if already is not None:
        return False
    clash = await session.scalar(
        select(User.id)
        .where(belongs_to(org_id), func.lower(User.email) == func.lower(user.email), User.id != user_id)
        .limit(1)
    )
    if clash is not None:
        raise MembershipError("email-taken")
    session.add(OrgMember(org_id=org_id, user_id=user_id))
    await session.flush()
    return True


async def remove_member(session: AsyncSession, org_id: UUID, user_id: UUID) -> bool:
    """End *user_id*'s membership of *org_id*; False when there is none.

    The user's rows in *org_id* go with it: the rows they own there are
    deleted and the rest, their sessions and history, are detached from
    them (:func:`surogates.db.agent_users.clear_user_rows`), so their
    home account can later be deleted. The account itself stays. A
    user's home organisation is not a membership: leaving it would
    leave the account with no home.
    """
    # Imported here: surogates.db.agent_users builds on this module.
    from surogates.db.agent_users import clear_user_rows

    home = await session.scalar(select(User.org_id).where(User.id == user_id))
    if home == org_id:
        raise MembershipError("home-org")
    await clear_user_rows(session, org_id=org_id, user_id=user_id)
    result = await session.execute(
        delete(OrgMember).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id)
    )
    return bool(result.rowcount)
