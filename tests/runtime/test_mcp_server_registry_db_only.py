"""Plan 5 / Task 8 source-level regression.

The filesystem fallback at ``/etc/surogates/mcp/`` (a mounted
ConfigMap in the legacy helm chart) is retired in favour of the
DB-only path.  The MCP loader must not read from
``/etc/surogates/mcp/`` at all anymore.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_load_mcp_configs_does_not_read_etc_surogates_mcp():
    """The literal '/etc/surogates/mcp' must not appear in any
    code path that load_mcp_configs reaches — anywhere in the
    mcp_proxy package or any helper it calls."""
    pattern = re.compile(r"/etc/surogates/mcp\b")
    offenders: list[str] = []
    for path in Path("surogates/mcp_proxy").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, (
        "Plan 5 retired the filesystem fallback; remove references:\n"
        + "\n".join(offenders)
    )


def test_loader_has_no_load_platform_configs_helper():
    """Plan 5 / Task 8.  The ``_load_platform_configs`` helper was
    the on-disk fallback's gateway.  Retiring it ensures a future
    refactor can't re-introduce a quiet filesystem read by
    delegating through the same helper."""
    import surogates.mcp_proxy.loader as loader

    assert not hasattr(loader, "_load_platform_configs"), (
        "Plan 5 retired _load_platform_configs; the DB-backed "
        "_load_db_configs is the only registry path now."
    )
