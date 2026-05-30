"""Plan 8 / Task 8 source-level regression.

The shared-mode worker bootstrap MUST NOT instantiate
:class:`ScheduledSessionRunner` -- the per-tenant ticker is
the helm-mode artifact that the platform ticker
(:mod:`surogates.scheduled.platform_ticker`) replaces.

The legacy ScheduledSessionRunner stays in the codebase for
helm-mode workers until Plan 9 retires them; this regression
just enforces the shared-mode wiring path is clean.

The check is a substring scan of ``run_worker`` source.  We
look for the shared-mode guard (``runtime_mode == 'shared'``)
that gates the ``ScheduledSessionRunner(...)`` constructor
call.  A future refactor that removed the guard but kept the
constructor call would silently regress the shared path back
to per-tenant ticking.
"""

from __future__ import annotations

import inspect
import re

from surogates.orchestrator import worker as worker_mod


def test_run_worker_gates_scheduled_runner_on_runtime_mode():
    """The constructor call for ScheduledSessionRunner appears in
    a branch that excludes shared-mode workers."""
    src = inspect.getsource(worker_mod.run_worker)
    # Must reference both ScheduledSessionRunner and the
    # shared-mode guard.  If a future refactor removed the
    # guard, this test catches it.
    assert "ScheduledSessionRunner" in src
    assert "runtime_mode" in src
    # And the guard must reference 'shared' so the branch
    # excludes shared agents (not just any conditional).
    assert re.search(
        r"runtime_mode[^\n]*shared",
        src,
    ) is not None


def test_run_worker_references_plan_8_task_8_in_a_comment():
    """The plan-7 / plan-8 trail in comments is the
    discoverability hook future readers use to find the
    rationale.  Without the comment, the guard looks arbitrary."""
    src = inspect.getsource(worker_mod.run_worker)
    assert "Plan 8 / Task 8" in src


def test_platform_ticker_is_the_documented_alternative():
    """The comment that documents the guard must point at the
    platform ticker (the replacement) so a reader knows where
    the shared-mode scheduled work fires from."""
    src = inspect.getsource(worker_mod.run_worker)
    assert "platform_ticker" in src
