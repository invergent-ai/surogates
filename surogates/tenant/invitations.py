"""Inviting a person into an organisation, and the link that lets a new password user set their password.

Keel invites staff through keel-link.  :func:`invite_member` finds the
organisation's user with the email, joins the one user of another
organisation who has it, or creates the account: a Firebase account in an
organisation with Firebase sign-in, linked by verified email at its first
sign-in, else a password account without a password.

A password account without a password whose home is the organisation gets
a set-password token.  The token is stateless: it carries a digest of the
account's password hash as it was when the token was minted, so it stops
working once a password is set, and :func:`read_set_password_token`
refuses it.  Nothing logs it or writes it down.

Only an account's home organisation sets its password: one password signs
in to every organisation the account belongs to, so an organisation the
account only joined could otherwise take it over.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import Org, User
from surogates.tenant.auth.firebase import firebase_auth_provider_name
from surogates.tenant.auth.jwt import (
    InvalidTokenError,
    create_set_password_token,
    decode_set_password_token,
)
from surogates.tenant.memberships import add_member, belongs_to

__all__ = [
    "SET_PASSWORD_DAYS",
    "InvalidSetPasswordToken",
    "Invitation",
    "InvitationError",
    "InvitationErrorCode",
    "invite_member",
    "read_set_password_token",
    "set_password_token",
]

#: How long a set-password token lasts.
SET_PASSWORD_DAYS = 7

#: The ``auth_provider`` of a password account.
_PASSWORD_PROVIDER = "database"
#: What every Firebase ``auth_provider`` starts with: ``firebase:<project>``.
_FIREBASE_PREFIX = "firebase:"
#: A set-password token is a few hundred characters: anything far longer is no token.
_MAX_TOKEN_CHARS = 4096

InvitationErrorCode = Literal["unknown-org", "ambiguous-email", "other-firebase-project"]


class InvitationError(Exception):
    """An invitation refused, for the reason its ``code`` names."""

    def __init__(self, code: InvitationErrorCode) -> None:
        super().__init__(code)
        self.code: InvitationErrorCode = code


class InvalidSetPasswordToken(Exception):
    """A set-password token refused.  It says nothing of why: every refusal answers alike."""


@dataclass(frozen=True)
class Invitation:
    user_id: UUID
    created: bool
    joined: bool
    sign_in: Literal["password", "firebase"]
    set_password_token: str | None
    expires_at: datetime | None


def _password_digest(password_hash: str | None, secret: str) -> str:
    """A keyed digest of a password hash, or of none: the token shows nothing of the hash itself."""
    message = b"set-password\x00" + (password_hash or "").encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def set_password_token(user: User, org_id: UUID, secret: str, now: datetime) -> tuple[str, datetime]:
    """A token that lets *user* set their password through *org_id* once, and when it lapses.

    It names the user, the organisation, its purpose and its expiry,
    :data:`SET_PASSWORD_DAYS` after *now*, and holds a digest of the user's
    password hash now.  It is signed with *secret*.
    """
    issued_at = int(now.timestamp())
    expires_at = issued_at + SET_PASSWORD_DAYS * 86400
    token = create_set_password_token(
        org_id=org_id,
        user_id=user.id,
        digest=_password_digest(user.password_hash, secret),
        issued_at=issued_at,
        expires_at=expires_at,
        secret=secret,
    )
    return token, datetime.fromtimestamp(expires_at, UTC)


async def read_set_password_token(
    session: AsyncSession, token: str, org_id: UUID, secret: str, now: datetime, *, lock: bool = True,
) -> User:
    """The user whose password *token* sets through *org_id*, locked until the caller's transaction ends.

    Refused, :class:`InvalidSetPasswordToken`, when the token does not
    verify with *secret* or has lapsed by *now*, names another organisation,
    names no password account whose home is *org_id*, or was minted before
    the account's password last changed.  The account's row is read ``FOR
    UPDATE``: of two calls with one token, the second waits for the first's
    transaction, and then sees the password it set.  With *lock* false it
    is read without the lock, for a check that writes nothing.
    """
    if len(token) > _MAX_TOKEN_CHARS:
        raise InvalidSetPasswordToken
    try:
        claims = decode_set_password_token(token, secret=secret, now=int(now.timestamp()))
        user_id = UUID(claims["sub"])
        token_org = UUID(claims["org_id"])
    except (InvalidTokenError, ValueError):
        raise InvalidSetPasswordToken from None
    if token_org != org_id:
        raise InvalidSetPasswordToken
    query = select(User).where(User.id == user_id, User.org_id == org_id)
    if lock:
        query = query.with_for_update()
    user = await session.scalar(query.execution_options(populate_existing=True))
    if user is None or user.auth_provider != _PASSWORD_PROVIDER:
        raise InvalidSetPasswordToken
    expected = _password_digest(user.password_hash, secret)
    if not hmac.compare_digest(expected.encode(), claims["pwd"].encode()):
        raise InvalidSetPasswordToken
    return user


def _sign_in(user: User, firebase_provider: str | None) -> Literal["password", "firebase"]:
    """How *user* signs in to an organisation whose Firebase ``auth_provider`` is *firebase_provider*.

    A Firebase account of another Firebase project, or one in an
    organisation with no Firebase, has no way in there: refused.
    """
    if not user.auth_provider.startswith(_FIREBASE_PREFIX):
        return "password"
    if user.auth_provider != firebase_provider:
        raise InvitationError("other-firebase-project")
    return "firebase"


def _invitation(
    user: User, org_id: UUID, *, created: bool, joined: bool, firebase_provider: str | None,
    secret: str, now: datetime,
) -> Invitation:
    sign_in = _sign_in(user, firebase_provider)
    token: str | None = None
    expires_at: datetime | None = None
    if user.auth_provider == _PASSWORD_PROVIDER and not user.password_hash and user.org_id == org_id:
        token, expires_at = set_password_token(user, org_id, secret, now)
    return Invitation(
        user_id=user.id, created=created, joined=joined, sign_in=sign_in,
        set_password_token=token, expires_at=expires_at,
    )


async def invite_member(
    session: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    display_name: str,
    firebase_project: str | None,
    secret: str,
    now: datetime,
) -> Invitation:
    """Find, join or create the user of *org_id* with *email*.

    The email is compared without case, and a new account's is stored
    trimmed and lowercased: the Firebase link and the password login find
    it as their sign-ins write it.  *firebase_project* is the organisation's
    Firebase project id, None when it has none.  An existing account is
    never changed: not its password, its way of signing in nor its name;
    joining only adds the membership.

    Raises :class:`InvitationError`: ``unknown-org``; ``ambiguous-email``
    when two or more users of other organisations have the email; and
    ``other-firebase-project`` for an account of a Firebase project the
    organisation does not sign in with.  A refusal writes nothing.  The
    caller commits.
    """
    email = email.strip().lower()
    firebase_provider = firebase_auth_provider_name(firebase_project) if firebase_project else None
    # Invitations into one organisation run one after another: the second
    # finds whom the first created or joined.
    if await session.scalar(select(Org.id).where(Org.id == org_id).with_for_update()) is None:
        raise InvitationError("unknown-org")

    member = await session.scalar(
        select(User)
        .where(belongs_to(org_id), func.lower(User.email) == email)
        .order_by((User.org_id == org_id).desc())
        .limit(1)
    )
    if member is not None:
        return _invitation(
            member, org_id, created=False, joined=False, firebase_provider=firebase_provider,
            secret=secret, now=now,
        )

    elsewhere = (await session.scalars(select(User).where(func.lower(User.email) == email).limit(2))).all()
    if len(elsewhere) > 1:
        raise InvitationError("ambiguous-email")
    if elsewhere:
        user = elsewhere[0]
        # Refused before anything is written.
        _sign_in(user, firebase_provider)
        await add_member(session, org_id, user.id)
        return _invitation(
            user, org_id, created=False, joined=True, firebase_provider=firebase_provider,
            secret=secret, now=now,
        )

    user = User(
        id=uuid.uuid4(),
        org_id=org_id,
        email=email,
        display_name=display_name.strip(),
        auth_provider=firebase_provider or _PASSWORD_PROVIDER,
        external_id=None,
        password_hash=None,
    )
    session.add(user)
    await session.flush()
    return _invitation(
        user, org_id, created=True, joined=False, firebase_provider=firebase_provider,
        secret=secret, now=now,
    )
