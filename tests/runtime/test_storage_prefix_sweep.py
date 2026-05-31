"""Source-level regression: no hot-path settings.tenant_assets_root reads.

The harness and tool-execution paths source their
storage prefix from the per-session TenantContext.asset_root.  Reading
``settings.tenant_assets_root`` (or any equivalent attribute access)
on the worker hot path silently routes every tenant through one
root.

Scope: the worker / harness / tools tree.  The api-side
``tenant/auth/middleware.py`` continues to build TenantContext from
``request.app.state.settings.tenant_assets_root`` because the auth
middleware runs before the per-request agent_runtime_context_dep can
populate ``ctx.storage_key_prefix``; that path is a later plan's
concern.

The test matches the *attribute access* pattern (``.tenant_assets_root``)
so the field definition in ``surogates/config.py`` and bare-word
mentions in comments / docstrings are tolerated.  The two allowlist
entries are the storage backends (which use the value as their own
on-disk root, not as a per-tenant prefix) and the api middleware
described above.
"""

from __future__ import annotations

import re
from pathlib import Path


_ALLOWLIST = {
    # LocalBackend / S3Backend factories use it as the storage-
    # backend-level base path, not a per-tenant prefix.  
    "surogates/storage/backend.py",
    # API auth middleware builds TenantContext from the JWT claims
    # *before* agent_runtime_context_dep can supply ctx.  Migration
    # of this path is a later plan's scope.
    "surogates/tenant/auth/middleware.py",
    # Scheduled idle-session reset cron — not on the worker hot
    # path (runs occasionally per the scheduled-work runner).
    # Platform-level scheduled work + CronJobs migrates
    # this to a per-session AgentRuntimeContext lookup.
    "surogates/jobs/reset_idle_sessions.py",
}


def _strip_comment(line: str) -> str:
    """Return the line content with any trailing ``# ...`` comment
    removed"""
    in_string: str | None = None
    for i, ch in enumerate(line):
        if in_string is not None:
            if ch == in_string and line[i - 1] != "\\":
                in_string = None
            continue
        if ch in ("'", '"'):
            in_string = ch
            continue
        if ch == "#":
            return line[:i]
    return line


def test_no_tenant_assets_root_reads_on_worker_hot_path():
    offenders: list[str] = []
    # Match attribute access: ``settings.tenant_assets_root``,
    # ``something.tenant_assets_root``.  Bare-word mentions in
    # comments / docstrings / field definitions are tolerated.
    pattern = re.compile(r"\.tenant_assets_root\b")
    for path in Path("surogates").rglob("*.py"):
        rel = str(path)
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            # Skip lines whose content (after string-aware comment
            # stripping) doesn't contain the pattern — catches our
            # own comments referencing the deprecated path.
            code = _strip_comment(raw_line)
            if pattern.search(code):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "These files read .tenant_assets_root on the worker hot "
        "path; route through TenantContext.asset_root or "
        "AgentRuntimeContext.storage_key_prefix instead:\n"
        + "\n".join(offenders)
    )
