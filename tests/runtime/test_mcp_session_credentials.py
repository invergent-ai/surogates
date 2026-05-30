"""Tests for per-session MCP credential resolution.

Plan 2 / Task 15.  The MCP loader today reads credentials from a
process-wide vault closure.  Plan 2 routes the lookup through a
per-session resolver so a credential rotation (admin re-vaults a
key while the worker is running) lands in the next session without
a worker restart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from surogates.mcp_proxy.session_credentials import (
    MCPServerCredentialRef,
    resolve_mcp_credentials,
)


@pytest.mark.asyncio
async def test_resolve_mcp_credentials_per_session_vault_call():
    """Each call asks the per-session vault — no module-level cache
    so a rotation between sessions is visible immediately."""
    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(
        side_effect=lambda ref, **_: f"sk-{ref[8:]}",
    )

    refs = [
        MCPServerCredentialRef(name="API_TOKEN", ref="vault://server-a-token"),
        MCPServerCredentialRef(name="DB_PASS", ref="vault://server-a-db"),
    ]
    resolved = await resolve_mcp_credentials(
        refs, vault=vault, org_id="o-1", user_id=None,
    )
    assert resolved == {
        "API_TOKEN": "sk-server-a-token",
        "DB_PASS": "sk-server-a-db",
    }
    assert vault.resolve_ref.await_count == 2


@pytest.mark.asyncio
async def test_resolve_mcp_credentials_empty_refs():
    """No refs to resolve — return an empty dict, do not error."""
    vault = AsyncMock()
    assert await resolve_mcp_credentials(
        [], vault=vault, org_id="o-1", user_id=None,
    ) == {}
    vault.resolve_ref.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_mcp_credentials_propagates_invalid_ref():
    """A malformed vault:// ref is a config error — surface it,
    don't silently drop the credential (the MCP server call would
    then fail with a confusing 'token not found' instead of
    'config malformed')."""
    from surogates.tenant.credentials import InvalidVaultRef

    vault = AsyncMock()
    vault.resolve_ref = AsyncMock(side_effect=InvalidVaultRef("bad"))

    with pytest.raises(InvalidVaultRef):
        await resolve_mcp_credentials(
            [MCPServerCredentialRef(name="X", ref="not-a-vault-ref")],
            vault=vault, org_id="o-1", user_id=None,
        )


def test_mcp_proxy_does_not_call_vault_retrieve_directly():
    """Plan 2 / Task 16 source-level regression.

    Every MCP credential read in ``surogates/mcp_proxy/`` must go
    through ``vault.resolve_ref`` (the single canonical per-session
    entry point) — no direct ``vault.retrieve`` calls.  A future
    refactor that re-introduces a direct read would be a step
    backward toward a process-wide cache and a credential-rotation
    blackout window.

    The regex matches the attribute-call pattern
    ``vault.retrieve(`` so the field name appearing in a comment or
    docstring does not trip the test.
    """
    import re
    from pathlib import Path

    pattern = re.compile(r"\bvault\.retrieve\(")
    offenders: list[str] = []
    for path in Path("surogates/mcp_proxy").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, (
        "These mcp_proxy files call vault.retrieve directly; route "
        "through vault.resolve_ref (the single per-session entry "
        "point used by resolve_mcp_credentials):\n"
        + "\n".join(offenders)
    )
