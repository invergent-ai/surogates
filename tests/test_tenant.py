"""Tests for surogates.tenant (context, JWT, assets)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from surogates.tenant.assets import TenantAssetManager
from surogates.tenant.auth.jwt import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from surogates.tenant.context import TenantContext, get_tenant, set_tenant


# =========================================================================
# TenantContext (contextvars)
# =========================================================================


class TestTenantContextVar:
    """set_tenant / get_tenant propagation."""

    def test_set_and_get_tenant(self, tenant_context: TenantContext):
        token = set_tenant(tenant_context)
        try:
            retrieved = get_tenant()
            assert retrieved.org_id == tenant_context.org_id
            assert retrieved.user_id == tenant_context.user_id
            assert retrieved.permissions == frozenset({"read", "write", "admin"})
        finally:
            # Reset to avoid polluting other tests.
            from surogates.tenant.context import _tenant_ctx
            _tenant_ctx.reset(token)

    def test_get_tenant_raises_when_not_set(self):
        # In a fresh context the var should not be set.
        # We rely on pytest running this in a context where set_tenant
        # was never called (or was reset).
        with pytest.raises(LookupError):
            get_tenant()


class TestTenantContextImmutability:
    """TenantContext is frozen (dataclass)."""

    def test_frozen(self, tenant_context: TenantContext):
        with pytest.raises(AttributeError):
            tenant_context.org_id = uuid4()  # type: ignore[misc]


# =========================================================================
# JWT tokens
# =========================================================================


class TestJWT:
    """JWT creation and validation."""

    def test_access_token_roundtrip(self):
        org_id = uuid4()
        user_id = uuid4()
        perms = {"read", "write"}

        token = create_access_token(org_id, user_id, perms)
        payload = decode_token(token)

        assert payload["org_id"] == str(org_id)
        assert payload["user_id"] == str(user_id)
        assert payload["type"] == "access"
        assert set(payload["permissions"]) == perms

    def test_refresh_token_roundtrip(self):
        org_id = uuid4()
        user_id = uuid4()

        token = create_refresh_token(org_id, user_id)
        payload = decode_token(token)

        assert payload["org_id"] == str(org_id)
        assert payload["user_id"] == str(user_id)
        assert payload["type"] == "refresh"
        assert payload["permissions"] == []

    def test_expired_token_raises(self):
        org_id = uuid4()
        user_id = uuid4()

        # Create a token that expired 1 minute ago.
        token = create_access_token(
            org_id, user_id, {"read"}, expires_minutes=-1
        )
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_access_token_has_expiry(self):
        org_id = uuid4()
        user_id = uuid4()
        token = create_access_token(org_id, user_id, set())
        payload = decode_token(token)
        assert "exp" in payload
        assert payload["exp"] > time.time()

    def test_refresh_token_has_long_expiry(self):
        org_id = uuid4()
        user_id = uuid4()
        token = create_refresh_token(org_id, user_id, expires_days=7)
        payload = decode_token(token)
        # Should expire ~7 days from now.
        assert payload["exp"] > time.time() + 6 * 86400


# =========================================================================
# TenantAssetManager
# =========================================================================


class TestTenantAssetManager:
    """Directory structure and provisioning."""

    def test_directory_structure(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")

        root = mgr.get_asset_root(org_id)
        assert str(org_id) in root

        user_root = mgr.get_user_asset_root(org_id, user_id)
        assert str(org_id) in user_root
        assert str(user_id) in user_root

    @pytest.mark.asyncio
    async def test_ensure_asset_dirs_creates_dirs(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")

        await mgr.ensure_asset_dirs(org_id, user_id)

        org_str = str(org_id)
        user_str = str(user_id)
        for subdir in ("memories", "skills", "mcp", "tools"):
            shared = tmp_path / org_str / "shared" / subdir
            assert shared.is_dir(), f"Missing: {shared}"
            user_dir = tmp_path / org_str / "users" / user_str / subdir
            assert user_dir.is_dir(), f"Missing: {user_dir}"

    @pytest.mark.asyncio
    async def test_ensure_asset_dirs_idempotent(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")

        # Call twice -- should not raise.
        await mgr.ensure_asset_dirs(org_id, user_id)
        await mgr.ensure_asset_dirs(org_id, user_id)

    def test_memory_dir(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")
        mem = mgr.memory_dir(org_id, user_id)
        assert "memories" in mem
        assert str(user_id) in mem

    def test_skills_dir_shared(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        skills = mgr.skills_dir(org_id)
        assert "shared" in skills
        assert "skills" in skills

    def test_skills_dir_user(self, tmp_path: Path):
        mgr = TenantAssetManager(str(tmp_path))
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")
        skills = mgr.skills_dir(org_id, user_id)
        assert str(user_id) in skills
        assert "skills" in skills
