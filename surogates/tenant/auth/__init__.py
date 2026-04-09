"""Pluggable authentication subsystem."""

from __future__ import annotations

from surogates.tenant.auth.base import AuthProvider, AuthResult
from surogates.tenant.auth.database import DatabaseAuthProvider
from surogates.tenant.auth.jwt import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from surogates.tenant.auth.middleware import get_current_tenant, setup_auth_middleware

__all__ = [
    # Protocol + result
    "AuthProvider",
    "AuthResult",
    # Providers
    "DatabaseAuthProvider",
    # JWT
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "InvalidTokenError",
    # Middleware / dependency
    "get_current_tenant",
    "setup_auth_middleware",
]
