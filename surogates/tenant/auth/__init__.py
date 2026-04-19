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
from surogates.tenant.auth.service_account import (
    IssuedServiceAccount,
    ServiceAccountStore,
    TOKEN_PREFIX as SERVICE_ACCOUNT_TOKEN_PREFIX,
    is_service_account_token,
)

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
    # Service accounts
    "IssuedServiceAccount",
    "ServiceAccountStore",
    "SERVICE_ACCOUNT_TOKEN_PREFIX",
    "is_service_account_token",
]
