"""Pluggable authentication provider interface.

Each organisation may configure a different backend (database, LDAP, OIDC).
All providers conform to the ``AuthProvider`` protocol so the rest of the
platform never depends on a concrete implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "AuthResult",
    "AuthProvider",
]


@dataclass
class AuthResult:
    """Outcome of an authentication or user-info lookup."""

    authenticated: bool
    user_id: str | None = None  # external provider user ID
    email: str | None = None
    display_name: str | None = None
    groups: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class AuthProvider(Protocol):
    """Structural protocol that every auth backend must satisfy."""

    async def authenticate(self, credentials: dict) -> AuthResult:
        """Verify *credentials* and return the result.

        ``credentials`` is a free-form dict whose schema depends on the
        concrete backend (e.g. ``{"email": ..., "password": ...}`` for the
        database provider).
        """
        ...  # pragma: no cover
