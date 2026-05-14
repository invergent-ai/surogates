"""Tests for ``require_not_channel_principal`` and the per-route gates
that use it.

The helper is the single point where routes refuse channel-session
principals before doing any tenant-storage work that would otherwise
inherit ``user_id=None → shared/*`` semantics.  Service-account
contexts (also ``user_id=None``) must NOT be refused — that's the
whole regression hazard the gate is shaped around.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes._shared import require_not_channel_principal
from surogates.tenant.context import TenantContext


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _channel_ctx(tmp_path: Path) -> TenantContext:
    """A channel-session context (only ``session_scope_id`` set)."""
    return TenantContext(
        org_id=uuid4(),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(tmp_path),
        service_account_id=None,
        session_scope_id=uuid4(),
    )


def _user_ctx(tmp_path: Path) -> TenantContext:
    return TenantContext(
        org_id=uuid4(),
        user_id=uuid4(),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(tmp_path),
        service_account_id=None,
        session_scope_id=None,
    )


def _sa_ctx(tmp_path: Path, *, session_scoped: bool = False) -> TenantContext:
    """A service-account context — bare or worker-minted session JWT.

    Both shapes have ``user_id=None`` and rely on
    ``TenantStorage(user_id=None) → shared/*`` for org-wide assets.
    The gate must NOT refuse them.
    """
    return TenantContext(
        org_id=uuid4(),
        user_id=None,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(tmp_path),
        service_account_id=uuid4(),
        session_scope_id=uuid4() if session_scoped else None,
    )


# ---------------------------------------------------------------------------
# Helper unit
# ---------------------------------------------------------------------------


class TestRequireNotChannelPrincipal:
    def test_user_context_allowed(self, tmp_path: Path):
        assert require_not_channel_principal(_user_ctx(tmp_path)) is None

    def test_bare_service_account_allowed(self, tmp_path: Path):
        assert require_not_channel_principal(_sa_ctx(tmp_path)) is None

    def test_sa_session_token_allowed(self, tmp_path: Path):
        """SA-session contexts have ``user_id=None`` but a valid SA;
        the helper must NOT confuse them with channel sessions."""
        assert (
            require_not_channel_principal(
                _sa_ctx(tmp_path, session_scoped=True),
            )
            is None
        )

    def test_channel_context_refused_with_403(self, tmp_path: Path):
        ctx = _channel_ctx(tmp_path)
        with pytest.raises(HTTPException) as exc:
            require_not_channel_principal(ctx)
        assert exc.value.status_code == 403
