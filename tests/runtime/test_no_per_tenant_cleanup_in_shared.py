"""Plan 8 / Task 12 source-level regression.

The per-tenant ``surogates.jobs.cleanup_sessions`` and
``surogates.jobs.reset_idle_sessions`` scripts MUST NOT be
imported at module load time from any shared-mode code path.
They stay in the codebase for helm-mode workers until Plan 9
retires them; this regression prevents the shared path from
accidentally re-using them in a way that defeats the platform-
level consolidation.

Module-level imports specifically (not function-body imports);
the production cleanup default DOES import cleanup_sessions
inside the function body because that's where the actual
per-agent body lives.  A function-body import only fires when
the default is exercised (which the Plan 8 follow-up wires);
a module-level import would create the load-time coupling we
want to prevent.
"""

from __future__ import annotations

import inspect


def test_platform_cleanup_module_does_not_import_per_tenant_script():
    """The platform_cleanup module body MUST NOT import
    surogates.jobs.cleanup_sessions at module load time."""
    from surogates.jobs import platform_cleanup

    src = inspect.getsource(platform_cleanup)
    # Find the module body (up to the first ``async def`` or
    # ``def``).  Comparing the full source would false-positive
    # on docstring mentions like the one this test asserts.
    body_end = min(
        (i for i in (
            src.find("\nasync def "),
            src.find("\ndef "),
        ) if i != -1),
        default=len(src),
    )
    module_body = src[:body_end]

    # The platform_cleanup module body imports asyncio,
    # logging, typing -- but MUST NOT import the per-tenant
    # cleanup script at module load time.
    assert "from surogates.jobs.cleanup_sessions" not in module_body
    assert "import surogates.jobs.cleanup_sessions" not in module_body


def test_platform_idle_reset_module_does_not_import_per_tenant_script():
    from surogates.jobs import platform_idle_reset

    src = inspect.getsource(platform_idle_reset)
    body_end = min(
        (i for i in (
            src.find("\nasync def "),
            src.find("\ndef "),
        ) if i != -1),
        default=len(src),
    )
    module_body = src[:body_end]

    assert (
        "from surogates.jobs.reset_idle_sessions" not in module_body
    )
    assert (
        "import surogates.jobs.reset_idle_sessions" not in module_body
    )


def test_platform_ticker_does_not_import_per_tenant_runner():
    """The platform ticker MUST NOT import the per-tenant
    ScheduledSessionRunner -- mixing the two ticking sources
    would defeat the leader-elected single-fire guarantee.

    We check for the import statement (not just the class
    name) because the platform_ticker docstring legitimately
    mentions ScheduledSessionRunner to explain what it
    replaces."""
    from surogates.scheduled import platform_ticker

    src = inspect.getsource(platform_ticker)
    assert "from surogates.scheduled.runner" not in src
    assert "import surogates.scheduled.runner" not in src
