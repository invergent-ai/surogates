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


# ---------------------------------------------------------------------------
# /v1/memory route gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMemoryRouteGate:
    """Channel principals must not read or write memory.

    Memory routes pass ``tenant.user_id`` straight to ``TenantStorage``,
    which maps ``user_id=None`` to ``shared/memory/*``.  The gate is the
    hard boundary that keeps channel JWTs out of shared memory while
    leaving service-account contexts (also ``user_id=None``) intact.
    """

    async def test_get_memory_refuses_channel_principal(
        self, tmp_path: Path,
    ):
        from surogates.api.routes import memory as memory_routes

        with pytest.raises(HTTPException) as exc:
            await memory_routes.get_memory(
                request=None,  # gate fires before request is read
                tenant=_channel_ctx(tmp_path),
            )
        assert exc.value.status_code == 403

    async def test_mutate_memory_refuses_channel_principal(
        self, tmp_path: Path,
    ):
        from surogates.api.routes import memory as memory_routes

        with pytest.raises(HTTPException) as exc:
            await memory_routes.mutate_memory(
                body=None,
                request=None,
                tenant=_channel_ctx(tmp_path),
            )
        assert exc.value.status_code == 403

    async def test_service_account_context_not_refused_by_gate(
        self, tmp_path: Path,
    ):
        """Regression guard: SA contexts (``user_id=None``) must still
        reach the handler body — ``TenantStorage`` will then route them
        to ``shared/memory/*``.  We verify the gate doesn't 403; the
        downstream call may fail because ``request.app.state.storage``
        isn't wired, but that's a different code path.
        """
        from surogates.api.routes import memory as memory_routes

        ctx = _sa_ctx(tmp_path)  # bare SA, user_id=None
        try:
            await memory_routes.get_memory(request=None, tenant=ctx)
        except HTTPException as exc:
            assert exc.status_code != 403, (
                "Gate must allow SA contexts with user_id=None"
            )
        except Exception:
            # Anything below the gate (AttributeError on request.app,
            # storage misconfig, ...) is acceptable — we only assert the
            # gate did not raise 403.
            pass


# ---------------------------------------------------------------------------
# Mutating /v1/skills route gating
# ---------------------------------------------------------------------------


_MUTATING_SKILL_HANDLERS = (
    "create_skill",
    "edit_skill",
    "patch_skill",
    "delete_skill",
    "write_skill_file",
    "remove_skill_file",
)


def _kwargs_for_handler(handler, tenant: TenantContext) -> dict:
    """Build kwargs satisfying *handler*'s signature.

    Required parameters that aren't ``tenant`` get ``None``; the gate
    must fire before any of them is dereferenced.  Parameters with
    defaults are omitted so FastAPI's ``Depends(...)`` defaults remain
    in place (and the gate uses the ``tenant`` kwarg we pass).
    """
    sig = inspect.signature(handler)
    kwargs: dict = {}
    for name, param in sig.parameters.items():
        if name == "tenant":
            kwargs[name] = tenant
        elif param.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[name] = None
    return kwargs


@pytest.mark.asyncio
class TestMutatingSkillsRouteGate:
    """Every mutate handler must refuse channel principals."""

    @pytest.mark.parametrize("handler_name", _MUTATING_SKILL_HANDLERS)
    async def test_refuses_channel_principal(
        self, tmp_path: Path, handler_name: str,
    ):
        from surogates.api.routes import skills as skills_routes

        handler = getattr(skills_routes, handler_name)
        kwargs = _kwargs_for_handler(handler, _channel_ctx(tmp_path))

        with pytest.raises(HTTPException) as exc:
            await handler(**kwargs)
        assert exc.value.status_code == 403, (
            f"{handler_name} must refuse channel principals"
        )



class TestReadOnlySkillsHandlersUngated:
    """``list_skills`` / ``view_skill`` / ``read_skill_file`` must
    remain accessible to channel principals — slash-skill expansion
    relies on them.  This is a sync source-inspection guard; intentionally
    not under the ``@pytest.mark.asyncio`` class above.
    """

    def test_read_only_handlers_do_not_call_gate(self):
        from surogates.api.routes import skills as skills_routes

        for fn_name in ("list_skills", "view_skill", "read_skill_file"):
            fn = getattr(skills_routes, fn_name)
            src = inspect.getsource(fn)
            assert "require_not_channel_principal" not in src, (
                f"{fn_name} must NOT call the gate; it is a read-only "
                "endpoint the channel JWT exists to unlock"
            )


# ---------------------------------------------------------------------------
# /v1/agents route gating (every handler)
# ---------------------------------------------------------------------------


_AGENT_HANDLERS = (
    "list_agents",
    "view_agent",
    "create_agent",
    "edit_agent",
    "delete_agent",
)


@pytest.mark.asyncio
class TestAgentsRouteGate:
    """Every ``/v1/agents`` handler refuses channel principals.

    Sub-agent definitions are deployment-private metadata; anonymous
    visitors must not enumerate or modify them even through a leaked
    channel JWT.
    """

    @pytest.mark.parametrize("handler_name", _AGENT_HANDLERS)
    async def test_refuses_channel_principal(
        self, tmp_path: Path, handler_name: str,
    ):
        from surogates.api.routes import agents as agents_routes

        handler = getattr(agents_routes, handler_name)
        kwargs = _kwargs_for_handler(handler, _channel_ctx(tmp_path))

        with pytest.raises(HTTPException) as exc:
            await handler(**kwargs)
        assert exc.value.status_code == 403, (
            f"{handler_name} must refuse channel principals"
        )
