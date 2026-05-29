"""Tests for Plan 4 memory-related AuditType entries.

Plan 4 / Task 1.  Per-user memory writes get audited so an admin
can trace 'what changed this user's memory' across the worker pool;
conflict events (concurrent writes to the same memory key) get a
distinct type so dashboards can surface the race rate per tenant.
"""

from __future__ import annotations

from surogates.audit.types import AuditType


def test_memory_write_type_exists():
    assert AuditType.MEMORY_WRITE.value == "memory.write"


def test_memory_conflict_type_exists():
    assert AuditType.MEMORY_CONFLICT.value == "memory.conflict"


def test_memory_types_are_unique():
    """The @unique decorator on the enum catches duplicate string
    values at class-definition time; this test just asserts the
    enum is loadable, which is enough to catch a copy-paste error
    that shipped two members with the same .value."""
    values = {m.value for m in AuditType}
    # +2 over the Plan 1 baseline (auth, MCP, credentials, copilot).
    assert len(values) >= 8
