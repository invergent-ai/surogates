"""Encrypted credential vault.

Secrets are encrypted at rest using Fernet (AES-128-CBC with HMAC-SHA256)
and stored in the ``credentials`` table.  Each credential is scoped to an
organisation and optionally to a specific user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from surogates.db.models import Credential

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

__all__ = ["CredentialVault", "InvalidVaultRef", "parse_vault_ref"]


class InvalidVaultRef(ValueError):
    """The ``vault://<credential>`` reference is malformed.

    Raised by :func:`parse_vault_ref` for any value that isn't
    exactly ``vault://`` followed by a non-empty credential name.
    The worker treats this as a configuration error — the runtime
    config is bad, the session cannot proceed."""


_VAULT_SCHEME = "vault://"


def parse_vault_ref(ref: str) -> str:
    """Extract the credential name from a ``vault://<name>`` reference.

    AgentRuntimeContext carries every API key as a
    ``vault://<credential>`` reference, never the raw value — secrets
    stay in the vault and the runtime-config payload that travels
    over HTTP to the worker only carries the reference.

    Returns the credential name (the substring after the scheme).
    Raises :class:`InvalidVaultRef` on any malformed input — empty
    string, missing scheme, wrong scheme, empty credential name.
    """
    if not ref or not ref.startswith(_VAULT_SCHEME):
        raise InvalidVaultRef(
            f"expected 'vault://<credential>'; got {ref!r}",
        )
    name = ref[len(_VAULT_SCHEME):]
    if not name:
        raise InvalidVaultRef(
            "vault reference has empty credential name",
        )
    return name


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

        Uses Postgres ``INSERT ... ON CONFLICT DO UPDATE`` keyed on the
        ``uq_credentials_org_user_name`` unique index so concurrent
        callers can't race past each other into duplicate rows.  ``xmax``
        is zero on inserted tuples and non-zero on updates — the canonical
        Postgres trick for detecting which branch fired.
        """
        encrypted = self._fernet.encrypt(value.encode("utf-8"))

        stmt = (
            pg_insert(Credential)
            .values(
                org_id=org_id,
                user_id=user_id,
                name=name,
                value_enc=encrypted,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "user_id", "name"],
                set_={"value_enc": encrypted},
            )
            .returning(
                Credential.id,
                literal_column("(xmax = 0)").label("inserted"),
            )
        )

        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(stmt)
                row = result.one()

        return row.id, bool(row.inserted)

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

    async def resolve_ref(
        self,
        ref: str,
        *,
        org_id: UUID,
        user_id: UUID | None = None,
    ) -> str | None:
        """Resolve a ``vault://<name>`` reference to plaintext.

        Wraps :meth:`retrieve` so the caller can
        hand the raw ``api_key_ref`` field from
        :class:`~surogates.runtime.LLMEndpoint` directly, without
        parsing the scheme itself.  Raises :class:`InvalidVaultRef`
        on a malformed reference.
        """
        name = parse_vault_ref(ref)
        return await self.retrieve(org_id, name, user_id=user_id)

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
