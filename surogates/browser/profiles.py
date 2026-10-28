"""Encrypted, principal-scoped browser login profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete as sa_delete
from sqlalchemy import any_, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import BrowserProfile


@dataclass(slots=True)
class BrowserProfileRow:
    id: UUID
    name: str
    source: str
    cookie_domains: list[str]
    created_at: datetime
    last_used_at: datetime | None
    has_state: bool
    use_in_channels: list[str] = field(default_factory=list)


def _row(p: BrowserProfile) -> BrowserProfileRow:
    return BrowserProfileRow(
        id=p.id,
        name=p.name,
        source=p.source,
        cookie_domains=list(p.cookie_domains or []),
        created_at=p.created_at,
        last_used_at=p.last_used_at,
        has_state=p.storage_state_enc is not None,
        use_in_channels=list(p.use_in_channels or []),
    )


def _cookie_domains(storage_state: dict) -> list[str]:
    seen: dict[str, None] = {}
    for cookie in storage_state.get("cookies", []) or []:
        domain = str(cookie.get("domain", "")).lstrip(".")
        if domain:
            seen.setdefault(domain, None)
    return sorted(seen)


class BrowserProfileStore:
    """CRUD + encrypted capture/inject for browser profiles.

    Every method takes ``(org_id, user_id, service_account_id)`` and filters on
    the exact principal so a profile is unreadable cross-principal even with a
    guessed id.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        encryption_key: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._fernet = Fernet(encryption_key)

    @staticmethod
    def _principal_clause(user_id: UUID | None, service_account_id: UUID | None):
        if (user_id is None) == (service_account_id is None):
            raise ValueError(
                "exactly one of user_id / service_account_id required"
            )
        if user_id is not None:
            return BrowserProfile.user_id == user_id
        return BrowserProfile.service_account_id == service_account_id

    async def create(
        self,
        org_id: UUID,
        *,
        user_id: UUID | None,
        service_account_id: UUID | None,
        name: str,
    ) -> BrowserProfileRow:
        self._principal_clause(user_id, service_account_id)
        profile = BrowserProfile(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            name=name,
        )
        async with self._session_factory() as s:
            async with s.begin():
                s.add(profile)
            await s.refresh(profile)
            return _row(profile)

    async def list(
        self,
        org_id: UUID,
        *,
        user_id: UUID | None,
        service_account_id: UUID | None,
    ) -> list[BrowserProfileRow]:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            rows = (
                await s.execute(
                    select(BrowserProfile)
                    .where(BrowserProfile.org_id == org_id, clause)
                    .order_by(BrowserProfile.created_at.asc())
                )
            ).scalars().all()
        return [_row(p) for p in rows]

    async def _get(
        self, s, profile_id, org_id, user_id, service_account_id
    ) -> BrowserProfile | None:
        clause = self._principal_clause(user_id, service_account_id)
        return (
            await s.execute(
                select(BrowserProfile).where(
                    BrowserProfile.id == profile_id,
                    BrowserProfile.org_id == org_id,
                    clause,
                )
            )
        ).scalar_one_or_none()

    async def rename(
        self, profile_id, org_id, *, user_id, service_account_id, name
    ) -> bool:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                    .values(name=name)
                )
        return result.rowcount > 0

    async def set_use_in_channels(
        self, profile_id, org_id, *, service_account_id, channels: list[str]
    ) -> bool:
        clause = self._principal_clause(None, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                    .values(use_in_channels=sorted(set(channels)))
                )
        return result.rowcount > 0

    async def used_in_channel(
        self, org_id, *, service_account_id, channel: str
    ) -> UUID | None:
        """The profile *service_account_id* (an agent) uses on *channel* unbound.

        The oldest one when several name it.
        """
        async with self._session_factory() as s:
            return (
                await s.execute(
                    select(BrowserProfile.id)
                    .where(
                        BrowserProfile.org_id == org_id,
                        BrowserProfile.service_account_id == service_account_id,
                        channel == any_(BrowserProfile.use_in_channels),
                        BrowserProfile.storage_state_enc.is_not(None),
                    )
                    .order_by(BrowserProfile.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def owners(
        self, org_id: UUID, profile_ids: list[UUID],
    ) -> dict[UUID, tuple[BrowserProfileRow, UUID | None, UUID | None]]:
        """Each of *profile_ids* in the org with its ``(user_id,
        service_account_id)`` owner; for callers that check the owner
        themselves (who may bind a profile, whose login a schedule uses)."""
        if not profile_ids:
            return {}
        async with self._session_factory() as s:
            rows = (
                await s.execute(
                    select(BrowserProfile).where(
                        BrowserProfile.org_id == org_id,
                        BrowserProfile.id.in_(profile_ids),
                    )
                )
            ).scalars().all()
        return {p.id: (_row(p), p.user_id, p.service_account_id) for p in rows}

    async def delete(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> bool:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    sa_delete(BrowserProfile).where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                )
        return result.rowcount > 0

    async def save_capture(
        self,
        profile_id,
        org_id,
        *,
        user_id,
        service_account_id,
        storage_state: dict,
    ) -> BrowserProfileRow:
        blob = self._fernet.encrypt(json.dumps(storage_state).encode("utf-8"))
        domains = _cookie_domains(storage_state)
        async with self._session_factory() as s:
            async with s.begin():
                profile = await self._get(
                    s, profile_id, org_id, user_id, service_account_id
                )
                if profile is None:
                    raise KeyError("profile not found")
                profile.storage_state_enc = blob
                profile.cookie_domains = domains
            await s.refresh(profile)
            return _row(profile)

    async def storage_state_for(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> dict | None:
        async with self._session_factory() as s:
            profile = await self._get(
                s, profile_id, org_id, user_id, service_account_id
            )
            if profile is None or profile.storage_state_enc is None:
                return None
            raw = profile.storage_state_enc
        try:
            return json.loads(self._fernet.decrypt(raw).decode("utf-8"))
        except InvalidToken:
            raise ValueError("failed to decrypt browser profile state")

    async def touch_last_used(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> None:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                await s.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                    # ``last_used_at`` is a naive ``timestamp without time
                    # zone`` column (like ``created_at``); a tz-aware
                    # ``datetime.now(timezone.utc)`` can't be encoded into it
                    # (asyncpg raises "can't subtract offset-naive and
                    # offset-aware datetimes"). Use the server clock, matching
                    # ``created_at``'s ``server_default=func.now()``.
                    .values(last_used_at=func.now())
                )
