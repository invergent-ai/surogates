"""The mail addresses of an organisation's users, and which of them mail can reach.

``users.email`` is a key as much as an address: the runtime stores stand-in addresses for users that have no mailbox, so
that the unique index on (org, lower(email)) holds. A channel user is
``<platform>-<id>@<org>.channels.surogate.local`` and a followed person is ``person-<id>@<org>.people.surogate.local``.
Both sit under :data:`STAND_IN_DOMAIN`. The runtime's agent addresses sit under ``.invalid``, which no mail reaches
(RFC 2606). :func:`deliverable` is the one place that knows this: a caller that mails a user asks it first.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import Org, User
from surogates.tenant.memberships import MembershipError, belongs_to

__all__ = ["STAND_IN_DOMAIN", "deliverable", "org_addresses"]

#: The domain every stand-in address of the runtime is under.
STAND_IN_DOMAIN = "surogate.local"


def deliverable(email: str) -> bool:
    """Whether mail can reach ``email``: it is ``local@domain``, and it is no stand-in and no ``.invalid`` address.

    ``.local`` in general stays deliverable: an on-premises mail domain can be ``.local``.
    """
    local, at, domain = email.partition("@")
    if not at or not local or "@" in domain or any(c.isspace() for c in email):
        return False
    domain = domain.lower().rstrip(".")
    if not domain:
        return False
    if domain == STAND_IN_DOMAIN or domain.endswith(f".{STAND_IN_DOMAIN}"):
        return False
    return domain.rsplit(".", 1)[-1] != "invalid"


async def org_addresses(db: AsyncSession, org_id: UUID, user_ids: Iterable[UUID]) -> dict[UUID, str]:
    """The address of each of *user_ids* that belongs to *org_id* and that mail can reach, as stored.

    A user belongs to the org as a home user or as a member. An id that names nobody, a user of another org and an
    address that is no mailbox are left out. Raises :class:`MembershipError` ``unknown-org`` when the org does not
    exist. It reads only: the caller commits nothing.
    """
    if await db.get(Org, org_id) is None:
        raise MembershipError("unknown-org")
    wanted = set(user_ids)
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(wanted), belongs_to(org_id)))
    return {user_id: email for user_id, email in rows if deliverable(email)}
