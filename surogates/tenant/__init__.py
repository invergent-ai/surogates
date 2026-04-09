"""Tenant isolation, authentication, and asset management."""

from __future__ import annotations

from surogates.tenant.assets import TenantAssetManager
from surogates.tenant.context import TenantContext, get_tenant, set_tenant
from surogates.tenant.credentials import CredentialVault
from surogates.tenant.models import (
    ChannelIdentityCreate,
    OrgCreate,
    OrgResponse,
    UserCreate,
    UserResponse,
)

__all__ = [
    # Context
    "TenantContext",
    "get_tenant",
    "set_tenant",
    # Models
    "OrgCreate",
    "OrgResponse",
    "UserCreate",
    "UserResponse",
    "ChannelIdentityCreate",
    # Services
    "CredentialVault",
    "TenantAssetManager",
]
