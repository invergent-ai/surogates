"""Encrypted credential vault.

Secrets are encrypted at rest using Fernet (AES-128-CBC with HMAC-SHA256)
and stored in the ``credentials`` table.  Each credential is scoped to an
organisation and optionally to a specific user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select

from surogates.db.models import Credential

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

__all__ = ["CredentialVault"]


class CredentialVault:
    """Encrypted credential storage backed by the ``credentials`` table.

    Parameters
    ----------
    session_factory:
        An ``async_sessionmaker`` bound to the platform database.
    encryption_key:
        A 32-byte URL-safe base64-encoded Fernet key.  Generate one with
        ``cryptography.fernet.Fernet.generate_key()``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        encryption_key: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._fernet = Fernet(encryption_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(
        self,
        org_id: UUID,
        name: str,
        value: str,
        user_id: UUID | None = None,
    ) -> tuple[UUID, bool]:
        """Encrypt *value* and store (or update) the named credential.

        Returns ``(credential_id, created)`` where ``created`` is
        ``True`` for a fresh insert and ``False`` when an existing row
        was replaced.  Callers use the flag to distinguish 201 vs 200
        responses without a second round trip.
        """
        encrypted = self._fernet.encrypt(value.encode("utf-8"))

        async with self._session_factory() as session:
            async with session.begin():
                existing = await self._get_credential(
                    session, org_id, name, user_id
                )
                if existing is not None:
                    existing.value_enc = encrypted
                    return existing.id, False

                credential = Credential(
                    org_id=org_id,
                    user_id=user_id,
                    name=name,
                    value_enc=encrypted,
                )
                session.add(credential)
                await session.flush()
                return credential.id, True

    async def retrieve(
        self,
        org_id: UUID,
        name: str,
        user_id: UUID | None = None,
    ) -> str | None:
        """Return the decrypted value of the named credential, or ``None``."""
        async with self._session_factory() as session:
            credential = await self._get_credential(
                session, org_id, name, user_id
            )

        if credential is None:
            return None

        try:
            return self._fernet.decrypt(credential.value_enc).decode("utf-8")
        except InvalidToken:
            raise ValueError(
                f"Failed to decrypt credential {name!r} for org {org_id}. "
                "The encryption key may have been rotated."
            )

    async def delete(
        self,
        org_id: UUID,
        name: str,
        user_id: UUID | None = None,
    ) -> bool:
        """Delete the named credential.  Returns ``True`` if it existed."""
        async with self._session_factory() as session:
            async with session.begin():
                stmt = delete(Credential).where(
                    Credential.org_id == org_id,
                    Credential.name == name,
                    self._user_id_clause(user_id),
                )
                result = await session.execute(stmt)
        return result.rowcount > 0  # type: ignore[union-attr]

    async def list_names(
        self,
        org_id: UUID,
        user_id: UUID | None = None,
    ) -> list[str]:
        """Return the names of all credentials for the given scope."""
        async with self._session_factory() as session:
            stmt = select(Credential.name).where(
                Credential.org_id == org_id,
                self._user_id_clause(user_id),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_all(
        self,
        user_id: UUID | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[tuple[UUID, UUID | None, str]], int]:
        """Platform-wide credential listing (admin use).

        Returns ``(rows, total)`` where rows are ``(org_id, user_id,
        name)`` tuples.  Plaintext is never loaded — only metadata.
        ``user_id`` filters to a specific user when supplied; pass
        ``None`` to include every row regardless of scope.
        """
        async with self._session_factory() as session:
            base = select(Credential)
            if user_id is not None:
                base = base.where(Credential.user_id == user_id)

            count_stmt = select(func.count()).select_from(base.subquery())
            total = (await session.execute(count_stmt)).scalar_one()

            page_stmt = (
                select(
                    Credential.org_id, Credential.user_id, Credential.name,
                )
                .order_by(Credential.org_id, Credential.name)
                .limit(limit)
                .offset(offset)
            )
            if user_id is not None:
                page_stmt = page_stmt.where(Credential.user_id == user_id)

            rows = (await session.execute(page_stmt)).all()

        return [(oid, uid, name) for (oid, uid, name) in rows], total

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _user_id_clause(user_id: UUID | None):
        """Return the appropriate SQLAlchemy clause for the user_id filter."""
        if user_id is not None:
            return Credential.user_id == user_id
        return Credential.user_id.is_(None)

    @classmethod
    async def _get_credential(
        cls,
        session,  # AsyncSession
        org_id: UUID,
        name: str,
        user_id: UUID | None,
    ) -> Credential | None:
        stmt = select(Credential).where(
            Credential.org_id == org_id,
            Credential.name == name,
            cls._user_id_clause(user_id),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
