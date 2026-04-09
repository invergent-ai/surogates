"""Shared fixtures for the Surogates test suite."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from surogates.tenant.context import TenantContext


# ---------------------------------------------------------------------------
# Ensure JWT secret is available for all tests that need it.
# ---------------------------------------------------------------------------
os.environ.setdefault("SUROGATES_JWT_SECRET", "test-secret-key-for-unit-tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tenant_context(tmp_path: Path) -> TenantContext:
    """A default TenantContext suitable for most unit tests."""
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={
            "agent_name": "TestAgent",
            "personality": "You are a helpful test assistant.",
            "default_model": "gpt-4o",
        },
        user_preferences={"theme": "dark", "language": "en"},
        permissions=frozenset({"read", "write", "admin"}),
        asset_root=str(tmp_path),
    )


@pytest.fixture()
def tmp_asset_root(tmp_path: Path) -> Path:
    """A temp directory structured as a tenant asset root.

    Layout::

        {tmp}/ORG_ID/shared/{memories,skills,mcp,tools}/
        {tmp}/ORG_ID/users/USER_ID/{memories,skills,mcp,tools}/
    """
    org_id = "00000000-0000-0000-0000-000000000001"
    user_id = "00000000-0000-0000-0000-000000000002"

    subdirs = ("memories", "skills", "mcp", "tools")
    shared_root = tmp_path / org_id / "shared"
    user_root = tmp_path / org_id / "users" / user_id

    for subdir in subdirs:
        (shared_root / subdir).mkdir(parents=True, exist_ok=True)
        (user_root / subdir).mkdir(parents=True, exist_ok=True)

    return tmp_path
