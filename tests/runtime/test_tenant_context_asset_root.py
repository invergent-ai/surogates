"""Tests for TenantContext.asset_root derivation.

Plan 2 / Task 9.  asset_root must derive from
AgentRuntimeContext.storage_key_prefix, not from process-wide
settings.tenant_assets_root — the worker pool serves many tenants
and a process-wide root is the wrong scope.
"""

from __future__ import annotations

from uuid import uuid4

from surogates.tenant.context import TenantContext


def test_tenant_context_accepts_explicit_asset_root():
    ctx = TenantContext(
        org_id=uuid4(), user_id=uuid4(),
        org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="p-1/a-1",
    )
    assert ctx.asset_root == "p-1/a-1"


def test_tenant_context_asset_root_is_just_the_storage_key_prefix():
    """No path-joining inside the constructor — the prefix is the
    asset root as-is.  Storage backends (S3 / local) layer their own
    bucket / base path on top.

    This contract matters because Plan 3 read-only file bundles also
    key on the same storage_key_prefix; the harness must read and
    write under the same root."""
    ctx = TenantContext(
        org_id=uuid4(), user_id=uuid4(),
        org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="p-1/a-1",
    )
    assert "/data" not in ctx.asset_root
    assert "tenant-assets" not in ctx.asset_root


def test_tenant_context_construction_does_not_read_settings():
    """TenantContext is constructed thousands of times per worker
    process; reading settings on every construction is both wrong
    (settings.tenant_assets_root is process-wide) and slow.  Plan 2
    forbids any settings touch in the constructor path."""
    import inspect

    # frozen dataclass __init__ is auto-generated; grep the source
    # file instead for a tenant_assets_root or settings reference.
    src = inspect.getsource(TenantContext)
    assert "tenant_assets_root" not in src
    assert "settings" not in src.lower() or "settings" not in src
