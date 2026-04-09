"""Database-backed authentication provider.

Looks up users by e-mail within the current organisation and verifies
passwords using bcrypt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import bcrypt as _bcrypt
from sqlalchemy import select

from surogates.db.models import User
from surogates.tenant.auth.base import AuthResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

__all__ = ["DatabaseAuthProvider"]


class DatabaseAuthProvider:
    """AuthProvider backed by the ``users`` table with bcrypt passwords."""

    def __init__(self, session_factory: async_sessionmaker, org_id: UUID) -> None:
        self._session_factory = session_factory
        self._org_id = org_id

    # ------------------------------------------------------------------
    # AuthProvider protocol
    # ------------------------------------------------------------------

    async def authenticate(self, credentials: dict) -> AuthResult:
        """Authenticate with ``{"email": ..., "password": ...}``."""
        email: str | None = credentials.get("email")
        password: str | None = credentials.get("password")

        if not email or not password:
            return AuthResult(
                authenticated=False,
                error="Both 'email' and 'password' are required.",
            )

        async with self._session_factory() as session:
            stmt = (
                select(User)
                .where(User.org_id == self._org_id, User.email == email)
            )
            result = await session.execute(stmt)
            user: User | None = result.scalar_one_or_none()

        if user is None:
            return AuthResult(authenticated=False, error="Invalid credentials.")

        if not user.password_hash:
            return AuthResult(
                authenticated=False,
                error="Account does not have a password configured.",
            )

        if not _bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
            return AuthResult(authenticated=False, error="Invalid credentials.")

        return AuthResult(
            authenticated=True,
            user_id=str(user.id),
            email=user.email,
            display_name=user.display_name,
        )

    async def get_user_info(self, user_id: str) -> AuthResult:
        """Look up a user by ``external_id`` (falls back to primary key)."""
        async with self._session_factory() as session:
            # Try external_id first, then primary key.
            stmt = (
                select(User)
                .where(User.org_id == self._org_id, User.external_id == user_id)
            )
            result = await session.execute(stmt)
            user: User | None = result.scalar_one_or_none()

            if user is None:
                # Fallback: treat user_id as the UUID primary key.
                try:
                    pk = UUID(user_id)
                except ValueError:
                    return AuthResult(authenticated=False, error="User not found.")

                stmt = select(User).where(User.org_id == self._org_id, User.id == pk)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()

        if user is None:
            return AuthResult(authenticated=False, error="User not found.")

        return AuthResult(
            authenticated=True,
            user_id=str(user.id),
            email=user.email,
            display_name=user.display_name,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def hash_password(plain: str) -> str:
        """Return a bcrypt hash suitable for storing in ``password_hash``."""
        return _bcrypt.hashpw(
            plain.encode("utf-8"), _bcrypt.gensalt(rounds=12)
        ).decode("utf-8")
