"""Refresh tokens of the public OAuth clients (Surogate Desktop), rotated on every use.

Each sign-in starts a family.  A refresh spends its token and issues the next
one in the same family, which keeps the sign-in's ``auth_time`` and computer.
A spent token presented again means the token has two holders, so the whole
family is revoked, and so is every token issued in it later (RFC 9700,
section 4.14.2).  A family ends FAMILY_LIFETIME after its browser sign-in,
and when the computer it is bound to is revoked (``surogates.devices.store``).
Only a token's SHA-256 digest is stored.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from surogates.db.models import OAuthRefreshToken
from surogates.tenant.auth.service_account import hash_token

REFRESH_PREFIX = "surg_rt_"
#: How long a refresh token lasts unused.
REFRESH_TTL = timedelta(days=30)
#: How long a sign-in lasts at all, from its browser sign-in: then the user signs in again.
FAMILY_LIFETIME = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RefreshGrant:
    """Whom a sign-in or a refresh signed in, its family, and its refresh token, which exists nowhere else."""

    org_id: UUID
    user_id: UUID
    auth_time: int
    family_id: UUID
    refresh_token: str


def _new_token() -> str:
    return REFRESH_PREFIX + secrets.token_urlsafe(32)


class OAuthTokens:
    """Issue, rotate and revoke refresh-token families in ``oauth_refresh_tokens``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def issue(
        self, *, org_id: UUID, user_id: UUID, agent_id: str, client_id: str, auth_time: int,
    ) -> RefreshGrant:
        """Start a sign-in's family, with its first refresh token."""
        token = _new_token()
        family_id = uuid.uuid4()
        async with self._sf() as db:
            db.add(self._row(token, family_id, org_id, user_id, agent_id, client_id, auth_time, None))
            await db.commit()
        return RefreshGrant(org_id, user_id, auth_time, family_id, token)

    async def rotate(self, token: str, *, client_id: str, agent_id: str) -> RefreshGrant | None:
        """Spend *token* for the next one in its family; None when it cannot be used.

        A token already spent revokes its whole family before None is returned.
        """
        digest = hash_token(token)
        revoked = aliased(OAuthRefreshToken)
        async with self._sf() as db:
            spent = (await db.execute(
                update(OAuthRefreshToken)
                .where(
                    OAuthRefreshToken.token_hash == digest,
                    OAuthRefreshToken.client_id == client_id,
                    OAuthRefreshToken.agent_id == agent_id,
                    OAuthRefreshToken.used_at.is_(None),
                    OAuthRefreshToken.revoked_at.is_(None),
                    OAuthRefreshToken.expires_at > func.now(),
                    OAuthRefreshToken.auth_time > int(time.time() - FAMILY_LIFETIME.total_seconds()),
                    # A family revoked once stays revoked, whichever of its tokens comes next.
                    ~exists().where(revoked.family_id == OAuthRefreshToken.family_id, revoked.revoked_at.is_not(None)),
                )
                .values(used_at=func.now())
                .returning(OAuthRefreshToken)
            )).scalar_one_or_none()
            if spent is None:
                reused = aliased(OAuthRefreshToken)
                await db.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.family_id.in_(
                        select(reused.family_id).where(reused.token_hash == digest, reused.used_at.is_not(None))
                    ))
                    .values(revoked_at=func.coalesce(OAuthRefreshToken.revoked_at, func.now()))
                )
                await db.commit()
                return None
            successor = _new_token()
            db.add(self._row(
                successor, spent.family_id, spent.org_id, spent.user_id, spent.agent_id, spent.client_id, spent.auth_time,
                spent.device_id,
            ))
            await db.commit()
        return RefreshGrant(spent.org_id, spent.user_id, spent.auth_time, spent.family_id, successor)

    async def bind(self, family_id: UUID, device_id: UUID) -> None:
        """Bind a sign-in to the computer it added or restored: revoking the computer ends it."""
        async with self._sf() as db:
            await db.execute(
                update(OAuthRefreshToken).where(OAuthRefreshToken.family_id == family_id).values(device_id=device_id)
            )
            await db.commit()

    async def revoke(self, token: str, *, client_id: str) -> None:
        """End *token*'s family, as signing out does. An unknown token changes nothing."""
        named = aliased(OAuthRefreshToken)
        async with self._sf() as db:
            await db.execute(
                update(OAuthRefreshToken)
                .where(OAuthRefreshToken.family_id.in_(
                    select(named.family_id).where(named.token_hash == hash_token(token), named.client_id == client_id)
                ))
                .values(revoked_at=func.coalesce(OAuthRefreshToken.revoked_at, func.now()))
            )
            await db.commit()

    @staticmethod
    def _row(
        token: str, family_id: UUID, org_id: UUID, user_id: UUID, agent_id: str, client_id: str, auth_time: int,
        device_id: UUID | None,
    ) -> OAuthRefreshToken:
        return OAuthRefreshToken(
            family_id=family_id,
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            client_id=client_id,
            device_id=device_id,
            token_hash=hash_token(token),
            auth_time=auth_time,
            expires_at=datetime.now(timezone.utc) + REFRESH_TTL,
        )
