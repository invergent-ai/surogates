"""Registered desktop devices.

Each row is one computer running Surogate Desktop for one agent and one
user.  The desktop authenticates its device link with a ``surg_dev_`` bearer
token; like ``surg_sk_`` keys, only the token's SHA-256 digest is stored, and
the raw token is returned once, on creation or on reauthorization.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import Device, DeviceOperation, OAuthRefreshToken
from surogates.tenant.auth.service_account import hash_token

TOKEN_PREFIX = "surg_dev_"
_DISPLAY_PREFIX_LEN = len(TOKEN_PREFIX) + 8
# 33 raw bytes -> 44 chars of base64url: 264 bits, as for surg_sk_ keys.
_SECRET_BYTES = 33

#: The outcome of an operation cancelled because its device was revoked.
REVOKED_OUTCOME = {"error": {"type": "revoked", "message": "Local access to this computer was revoked"}}


def generate_token() -> str:
    """Return a freshly minted device token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    id: UUID
    org_id: UUID
    agent_id: str
    user_id: UUID
    name: str
    token_prefix: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    credential_generation: int


@dataclass(frozen=True, slots=True)
class IssuedDevice:
    """A device and its raw token, which exists nowhere else."""

    device: DeviceRecord
    token: str


def _record(row: Device) -> DeviceRecord:
    return DeviceRecord(
        id=row.id,
        org_id=row.org_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        name=row.name,
        token_prefix=row.token_prefix,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        credential_generation=row.credential_generation,
    )


def _owned_by(org_id: UUID, agent_id: str, user_id: UUID) -> tuple[Any, ...]:
    return (Device.org_id == org_id, Device.agent_id == agent_id, Device.user_id == user_id)


class DeviceStore:
    """CRUD for ``devices``.  Token secrets are hashed before they reach the database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self, *, org_id: UUID, agent_id: str, user_id: UUID, name: str,
    ) -> IssuedDevice:
        token = generate_token()
        row = Device(
            org_id=org_id,
            agent_id=agent_id,
            user_id=user_id,
            name=name,
            token_hash=hash_token(token),
            token_prefix=token[:_DISPLAY_PREFIX_LEN],
        )
        async with self._sf() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return IssuedDevice(_record(row), token)

    async def list_for_user(
        self, *, org_id: UUID, agent_id: str, user_id: UUID,
    ) -> list[DeviceRecord]:
        """The user's devices for this agent, newest first, revoked ones included."""
        async with self._sf() as db:
            rows = (await db.execute(
                select(Device)
                .where(*_owned_by(org_id, agent_id, user_id))
                .order_by(Device.created_at.desc())
            )).scalars().all()
        return [_record(row) for row in rows]

    async def find(
        self, device_id: UUID, *, org_id: UUID, agent_id: str, user_id: UUID,
    ) -> DeviceRecord | None:
        """The user's device for this agent, revoked or not, or None if they have no such device."""
        async with self._sf() as db:
            row = (await db.execute(
                select(Device).where(Device.id == device_id, *_owned_by(org_id, agent_id, user_id))
            )).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def revoke(
        self, device_id: UUID, *, org_id: UUID, agent_id: str, user_id: UUID, by_sign_in: UUID | None = None,
    ) -> DeviceRecord | None:
        """Revoke the user's device.  Revoking again keeps the first revocation time.

        *by_sign_in* is the caller's OAuth sign-in, if any.  When it is the one
        bound to a device the link never welcomed, it is unbound rather than
        ended, and the device is deleted: the desktop taking back a computer it
        could not keep stays signed in, and leaves no row behind.
        """
        return await self._revoke(
            Device.id == device_id, *_owned_by(org_id, agent_id, user_id), by_sign_in=by_sign_in,
        )

    async def revoke_by_id(self, device_id: UUID, generation: int) -> DeviceRecord | None:
        """Revoke a device at its own request, made over its authenticated link.

        Only the credentials the link connected with may do it: after a
        reauthorization, a socket still open under the old token matches no row.
        """
        return await self._revoke(
            Device.id == device_id, Device.credential_generation == generation,
        )

    async def _revoke(self, *where: Any, by_sign_in: UUID | None = None) -> DeviceRecord | None:
        async with self._sf() as db:
            row = (await db.execute(
                update(Device)
                .where(*where)
                .values(revoked_at=func.coalesce(Device.revoked_at, func.now()))
                .returning(Device)
            )).scalar_one_or_none()
            if row is not None:
                if by_sign_in is not None and row.connected_at is None:
                    unbound = await db.execute(
                        update(OAuthRefreshToken)
                        .where(OAuthRefreshToken.family_id == by_sign_in, OAuthRefreshToken.device_id == row.id)
                        .values(device_id=None)
                    )
                    # Never welcomed, so it ran nothing: its operations, if any, go with it.
                    if unbound.rowcount:
                        await db.execute(delete(Device).where(Device.id == row.id))
                # The sign-in that added or restored the computer ends with it:
                # a lost laptop's copy of the app can no longer refresh.
                await db.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.device_id == row.id)
                    .values(revoked_at=func.coalesce(OAuthRefreshToken.revoked_at, func.now()))
                )
                # Revocation cancels the device's queued and running work, so a
                # later reauthorization cannot run it.  Waiters see the outcome
                # at their next recheck.
                await db.execute(
                    update(DeviceOperation)
                    .where(
                        DeviceOperation.device_id == row.id,
                        DeviceOperation.completed_at.is_(None),
                    )
                    .values(outcome=REVOKED_OUTCOME, completed_at=func.now())
                )
            await db.commit()
        return _record(row) if row is not None else None

    async def reauthorize(
        self, device_id: UUID, *, org_id: UUID, agent_id: str, user_id: UUID,
    ) -> IssuedDevice | None:
        """Issue a new token for the same device, restoring it if it was revoked.

        The old token stops working and ``credential_generation`` advances, so a
        socket still open under the old token can be closed.
        """
        token = generate_token()
        async with self._sf() as db:
            row = (await db.execute(
                update(Device)
                .where(Device.id == device_id, *_owned_by(org_id, agent_id, user_id))
                .values(
                    token_hash=hash_token(token),
                    token_prefix=token[:_DISPLAY_PREFIX_LEN],
                    revoked_at=None,
                    credential_generation=Device.credential_generation + 1,
                )
                .returning(Device)
            )).scalar_one_or_none()
            await db.commit()
        return IssuedDevice(_record(row), token) if row is not None else None

    async def find_by_token(self, token: str) -> DeviceRecord | None:
        """The device *token* belongs to in any state, or None when it is unknown.

        A revoked device is returned too, so callers must check ``revoked_at``.
        A token replaced by reauthorization is unknown: its hash is gone.
        """
        if not token.startswith(TOKEN_PREFIX):
            return None
        async with self._sf() as db:
            row = (await db.execute(
                select(Device).where(Device.token_hash == hash_token(token))
            )).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def welcomed(self, device_id: UUID) -> None:
        """Record that the link welcomed the device: from then on it has connected."""
        async with self._sf() as db:
            await db.execute(
                update(Device)
                .where(Device.id == device_id, Device.connected_at.is_(None))
                .values(connected_at=func.now())
            )
            await db.commit()

    async def touch(self, device_id: UUID) -> DeviceRecord | None:
        """Record that the device was seen now; return its row, or None if it is gone."""
        async with self._sf() as db:
            row = (await db.execute(
                update(Device)
                .where(Device.id == device_id)
                .values(last_seen_at=func.now())
                .returning(Device)
            )).scalar_one_or_none()
            await db.commit()
        return _record(row) if row is not None else None
