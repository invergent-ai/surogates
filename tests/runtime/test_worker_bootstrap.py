"""Tests for worker bootstrap branching on runtime_mode.

Exercises only the parts of
``orchestrator/worker.py`` that decide whether the SUROGATES_AGENT_ID
guard fires, whether the per-session agent-id mismatch raises, and
whether the harness factory closes over process-wide ids vs
per-session ids.

The worker entrypoint is large and pulls in many integration pieces,
so the tests here focus on the smallest decision points expressed as
pure functions and module-level branches.
"""

from __future__ import annotations

import inspect

import pytest


def _read_worker_source() -> str:
    import surogates.orchestrator.worker as w
    return inspect.getsource(w)


def test_helm_mode_still_requires_agent_id_in_source():
    """Regression: the helm path must keep raising when agent_id is
    unset."""
    src = _read_worker_source()
    # The error message text is load-bearing — operators grep on it.
    assert "SUROGATES_AGENT_ID is not set" in src
    assert "SUROGATES_ORG_ID is not set" in src


def test_shared_mode_branch_is_present_in_source():
    """Regression: the bootstrap branches on runtime_mode and
    explicitly clears configured ids in shared mode."""
    src = _read_worker_source()
    assert 'runtime_mode == "helm"' in src
    # Shared-mode bootstrap leaves both configured ids None so the
    # harness factory cannot accidentally use them.
    assert "configured_org_id = None" in src
    assert "configured_agent_id = None" in src


def test_mismatch_check_is_gated_on_helm_mode():
    """The defence-in-depth check fires only in helm mode."""
    src = _read_worker_source()
    # Look for a guarded conditional that includes both the helm
    # check and the mismatch test.  This is intentionally a source-
    # level assertion — the worker entry path is too large to spin
    # up in a unit test, but the gating logic is small enough to
    # validate textually.
    assert (
        'runtime_mode == "helm"' in src
        and "session.agent_id != configured_agent_id" in src
    )


def test_session_org_id_is_resolved_per_session_in_shared_mode():
    """The harness factory rebuilds the TenantContext from the
    *session* org in shared mode, never from process-wide
    configured_org_id.

    asset_root no longer reads
    settings.tenant_assets_root inline; it sources from the
    AgentRuntimeContext.storage_key_prefix (helm mode's
    _legacy_helm_context populates the field from
    settings.tenant_assets_root + org_id; shared mode reads from
    the runtime-config payload).  Regression below pins the new
    contract so a future refactor can't quietly re-add a
    process-wide read."""
    src = _read_worker_source()
    assert "session_org_id" in src
    # In shared mode the value is derived from the session row.
    assert "UUID(str(session.org_id))" in src
    # The TenantContext uses session_org_id, not configured_org_id.
    assert "org_id=session_org_id" in src
    # asset_root comes from ctx.storage_key_prefix.
    assert "asset_root=ctx.storage_key_prefix" in src
    # Belt-and-suspenders: the old process-wide path is gone.
    assert (
        'asset_root=f"{settings.tenant_assets_root}/{session_org_id}"'
        not in src
    )


@pytest.mark.asyncio
async def test_settings_runtime_mode_default_is_helm():
    """A bare ``Settings()`` instantiation continues to behave as a
    helm-mode pod so existing tests that construct Settings() without
    overrides keep working."""
    from surogates.config import Settings

    s = Settings()
    assert s.runtime_mode == "helm"
