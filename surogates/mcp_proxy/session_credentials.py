"""Per-session MCP server credential resolution.

Plan 2 / Task 15.  MCP server definitions reference credentials by
``vault://<name>`` — the worker resolves them to plaintext per
session at connect time.  Process-wide caching is intentionally
absent: a credential rotation by an admin should land in the next
session without a worker restart, and MCP server connect happens
once per session so the cost is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "MCPServerCredentialRef",
    "resolve_mcp_credentials",
]


@dataclass(frozen=True, slots=True)
class MCPServerCredentialRef:
    """One credential the MCP server needs: an env var / header
    name + a vault reference.  The MCP server config block
    contains a list of these."""

    name: str
    ref: str


async def resolve_mcp_credentials(
    refs: list[MCPServerCredentialRef],
    *,
    vault: Any,
    org_id: Any,
    user_id: Any = None,
) -> dict[str, str]:
    """Resolve a list of vault refs to a ``{name: plaintext}`` map.

    Raises :class:`~surogates.tenant.credentials.InvalidVaultRef` on
    any malformed reference — the caller (MCP loader) treats this
    as a configuration error and refuses to connect rather than
    silently dropping the credential."""
    resolved: dict[str, str] = {}
    for ref in refs:
        resolved[ref.name] = await vault.resolve_ref(
            ref.ref, org_id=org_id, user_id=user_id,
        )
    return resolved
