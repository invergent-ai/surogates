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


def _read_worker_source() -> str:
    import surogates.orchestrator.worker as w
    return inspect.getsource(w)


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


