"""Unit-tier shape test for ScheduledSessionStore.find_due_across_tenants.

Plan 8 / Task 4.  The full behavioural test lives at
``tests/integration/test_scheduled_store.py`` and requires
Postgres; this lightweight test exercises the method shape +
source-level guarantees without standing up a DB.

Tests:

1. The method exists on the store.
2. Its compiled SQL drops the ``agent_id =`` filter (the whole
   point of the multi-tenant variant -- if a future refactor
   re-adds the filter, this regression catches it).
"""

from __future__ import annotations

import inspect


def test_find_due_across_tenants_exists_on_store():
    from surogates.scheduled.store import ScheduledSessionStore

    assert hasattr(ScheduledSessionStore, "find_due_across_tenants")


def test_find_due_across_tenants_sql_omits_agent_id_filter():
    """Source-level regression: the multi-tenant query MUST NOT
    include an ``agent_id = ...`` WHERE clause -- otherwise it
    behaves like the per-tenant ``claim_due`` and the platform
    ticker would only find work for one agent."""
    from surogates.scheduled.store import ScheduledSessionStore

    src = inspect.getsource(
        ScheduledSessionStore.find_due_across_tenants,
    )
    assert "agent_id =" not in src
    assert "agent_id=:agent_id" not in src
    assert "FOR UPDATE SKIP LOCKED" in src


def test_claim_due_still_filters_by_agent_id():
    """The per-tenant ``claim_due`` MUST still filter by
    agent_id -- helm-mode workers depend on it.  This regression
    catches a future refactor that 'simplifies' the two methods
    into one and drops the helm-mode filter."""
    from surogates.scheduled.store import ScheduledSessionStore

    src = inspect.getsource(ScheduledSessionStore.claim_due)
    assert "agent_id = :agent_id" in src
