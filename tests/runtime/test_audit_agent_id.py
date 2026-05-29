"""Tests for AuditStore.emit recording agent_id.

Plan 1b / Task 16.  Every audit event must carry the
``(org_id, agent_id)`` tuple so downstream dashboards can filter by
tenant.  Helm-mode pods supply the agent_id from settings; shared-
mode pods from the per-request AgentRuntimeContext.  The column is
nullable so legacy emitters (and the few audit events that genuinely
have no agent context — platform copilot writes in helm mode) keep
working.

The unit tests in this module use an in-memory fake session factory
because the real ``audit_log`` table lives in PostgreSQL and the
existing integration suite (testcontainers) already exercises the
SQL round-trip.  The value of these tests is the surface contract of
``AuditStore.emit`` and the ORM-level column metadata; both are
DB-independent.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from surogates.audit.store import AuditStore
from surogates.audit.types import AuditType
from surogates.db.models import AuditLog


class _FakeSession:
    """Minimal async session that records every ``db.add(row)`` and
    assigns a row id on ``db.flush()``.  Sufficient for verifying
    that ``AuditStore.emit`` populates the agent_id field on the
    AuditLog instance before persistence."""

    _next_id = 1

    def __init__(self, captured: list[AuditLog]) -> None:
        self._captured = captured

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    def add(self, row: AuditLog) -> None:
        self._captured.append(row)

    async def flush(self) -> None:
        for row in self._captured:
            if row.id is None:
                row.id = _FakeSession._next_id
                _FakeSession._next_id += 1

    async def commit(self) -> None:
        return None


def _factory(captured: list[AuditLog]):
    def make() -> _FakeSession:
        return _FakeSession(captured)
    return make


@pytest.mark.asyncio
async def test_audit_emit_records_agent_id():
    captured: list[AuditLog] = []
    store = AuditStore(_factory(captured))
    org_id = uuid4()

    row_id = await store.emit(
        org_id=org_id,
        type=AuditType.AUTH_LOGIN,
        data={"k": "v"},
        agent_id="agent-x",
    )
    assert row_id is not None
    assert len(captured) == 1
    assert captured[0].agent_id == "agent-x"
    assert captured[0].org_id == org_id


@pytest.mark.asyncio
async def test_audit_emit_accepts_none_agent_id():
    """Helm-mode emitters that cannot reach an agent_id pass None.
    The column is nullable so the row still persists — old call
    sites (and emitters of legitimately tenant-less events like
    platform-copilot writes in helm mode) must keep working."""
    captured: list[AuditLog] = []
    store = AuditStore(_factory(captured))

    row_id = await store.emit(
        org_id=uuid4(),
        type=AuditType.AUTH_LOGIN,
        data={},
        agent_id=None,
    )
    assert row_id is not None
    assert captured[0].agent_id is None


@pytest.mark.asyncio
async def test_audit_emit_defaults_agent_id_to_none():
    """A caller that omits agent_id entirely must still write a row
    with NULL agent_id rather than raising — backwards-compat for
    callers that have not yet been migrated in Task 17."""
    captured: list[AuditLog] = []
    store = AuditStore(_factory(captured))

    row_id = await store.emit(
        org_id=uuid4(), type=AuditType.AUTH_LOGIN, data={},
    )
    assert row_id is not None
    assert captured[0].agent_id is None


def test_audit_log_model_has_agent_id_column_nullable_indexed():
    """Source-level regression: the AuditLog ORM model has an
    ``agent_id`` column, it is nullable (helm-mode emitters need
    None), and it's covered by an index so dashboards can filter
    by tenant without table-scanning."""
    col = AuditLog.__table__.c.agent_id
    assert col.nullable is True

    index_names = {idx.name for idx in AuditLog.__table__.indexes}
    assert any(
        "agent" in name.lower() for name in index_names
    ), f"AuditLog must have a per-agent index; have {index_names}"


def _extract_call_arguments(text: str, opening_paren_idx: int) -> str:
    """Return everything inside the matched ``audit_store.emit(...)``
    by walking parens with a depth counter — so nested calls inside
    the argument list (``data=rug_pull_event(...)``) do not close
    the outer call prematurely.

    String/comment-aware parsing would be more robust, but neither
    Python parens nor matching ``)`` characters legitimately appear
    inside strings in the audit_store.emit call sites we ship, so
    the depth counter is sufficient and avoids pulling in ``ast``.
    """
    depth = 1
    i = opening_paren_idx + 1
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[opening_paren_idx + 1 : i]
        i += 1
    raise AssertionError(
        f"Unmatched ( at offset {opening_paren_idx} — "
        "audit_store.emit call is malformed",
    )


def test_every_audit_emit_call_passes_agent_id():
    """Plan 1b / Task 17 source-level regression.

    Every ``audit_store.emit(...)`` call in the ``surogates/`` source
    tree must pass ``agent_id=`` explicitly (even if the value is
    ``None``).  This catches a future emitter — added by a refactor
    or a new auth provider — that silently drops the per-tenant
    column from the audit row.

    We grep the source rather than instrumenting each call so the
    test stays fast (no DB or app boot) and so a previously-missed
    site fails *the next time someone runs the tests*, not eventually
    in a dashboard.
    """
    import re
    from pathlib import Path

    call_re = re.compile(r"audit_store\.emit\(")
    missing: list[str] = []
    for path in Path("surogates").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in call_re.finditer(text):
            opening = m.end() - 1  # the '('
            args = _extract_call_arguments(text, opening)
            if "agent_id=" not in args:
                line = text[: m.start()].count("\n") + 1
                missing.append(f"{path}:{line}")
    assert not missing, (
        "These audit_store.emit(...) calls must pass agent_id= "
        "(use None if the emitter has no tenant context):\n"
        + "\n".join(missing)
    )


def test_mcp_proxy_loader_credential_access_emits_agent_id():
    """Plan 5 / Task 5.  Plan 1b Task 17 left this emit at
    ``agent_id=None`` because the loader had no agent_id in scope.
    Plan 5 threads it through the call stack from the proxy
    routes (where agent_runtime_context_dep resolves it)."""
    import inspect

    import surogates.mcp_proxy.loader as loader

    src = inspect.getsource(loader._emit_credential_access)
    sig = inspect.signature(loader._emit_credential_access)
    assert "agent_id" in sig.parameters
    assert "agent_id=None" not in src


def test_audit_log_observability_sql_retrofits_agent_id():
    """The idempotent DDL block in ``observability.sql`` retrofits
    existing deployed ``audit_log`` tables with the new column +
    index.  We grep the SQL rather than execute it so this test
    doesn't need a PostgreSQL instance."""
    from pathlib import Path

    sql = Path(
        "surogates/db/observability.sql",
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE audit_log" in sql
    assert "ADD COLUMN IF NOT EXISTS agent_id" in sql
    assert "idx_audit_log_agent_time" in sql
