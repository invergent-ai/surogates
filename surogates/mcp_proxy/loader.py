"""MCP server config loading with credential resolution.

Merges MCP server definitions from three layers (platform < org < user)
and resolves ``credential_refs`` from the encrypted credential vault,
injecting secrets into the server's ``env`` (stdio) or ``headers``
(HTTP) dicts.

Credential refs support two formats:

Simple string::

    credential_refs: ["GITHUB_TOKEN"]
    # Resolves credential named "GITHUB_TOKEN"
    # stdio: injected as env.GITHUB_TOKEN
    # http:  injected as headers.Authorization = "Bearer <value>"

Structured object::

    credential_refs:
      - name: "MY_TOKEN"
        env: "GITHUB_PERSONAL_ACCESS_TOKEN"
      - name: "API_KEY"
        header: "X-API-Key"
      - name: "AUTH"
        header: "Authorization"
        prefix: "Bearer "
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import McpServer
from surogates.tenant.credentials import CredentialVault
from surogates.tools.loader import ResourceLoader

logger = logging.getLogger(__name__)


async def load_mcp_configs(
    org_id: UUID,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    vault: CredentialVault,
    platform_mcp_dir: str,
) -> dict[str, dict[str, Any]]:
    """Load, merge, and credential-resolve MCP server configs.

    Returns a dict of ``{server_name: config_dict}`` ready for
    ``MCPServerTask`` / ``discover_mcp_tools()``.

    Merge precedence: platform < org-wide < user-specific.
    Disabled servers are excluded.
    """
    # 1. Platform configs from filesystem.
    platform_configs = _load_platform_configs(platform_mcp_dir)

    # 2. DB configs (org-wide + user-specific).
    db_configs = await _load_db_configs(session_factory, org_id, user_id)

    # 3. Merge: platform < org < user (higher precedence overwrites).
    merged: dict[str, dict[str, Any]] = {}
    merged.update(platform_configs)
    merged.update(db_configs)

    # 4. Resolve credential_refs in parallel across all servers.
    resolve_tasks = []
    for server_name, config in merged.items():
        credential_refs = config.pop("credential_refs", [])
        if credential_refs:
            resolve_tasks.append(
                _resolve_credentials_safe(
                    server_name, config, credential_refs, vault, org_id, user_id,
                )
            )

    if resolve_tasks:
        await asyncio.gather(*resolve_tasks)

    return merged


async def _resolve_credentials_safe(
    server_name: str,
    config: dict[str, Any],
    credential_refs: list[Any],
    vault: CredentialVault,
    org_id: UUID,
    user_id: UUID,
) -> None:
    """Wrapper that catches and logs credential resolution failures."""
    try:
        await _resolve_credentials(config, credential_refs, vault, org_id, user_id)
    except Exception:
        logger.exception(
            "Failed to resolve credentials for MCP server %s", server_name,
        )


def _load_platform_configs(platform_mcp_dir: str) -> dict[str, dict[str, Any]]:
    """Load MCP server definitions from the platform volume."""
    loader = ResourceLoader(platform_mcp_dir=platform_mcp_dir)
    configs: dict[str, dict[str, Any]] = {}

    for server_def in loader._load_mcp_from_dir(platform_mcp_dir):
        configs[server_def.name] = {
            "transport": server_def.transport,
            "command": server_def.command,
            "args": server_def.args,
            "url": server_def.url,
            "env": dict(server_def.env),
            "timeout": server_def.timeout,
        }

    return configs


async def _load_db_configs(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: UUID,
    user_id: UUID,
) -> dict[str, dict[str, Any]]:
    """Load MCP server configs from the database.

    Single query fetches org-wide (user_id IS NULL) and user-specific
    rows.  User-specific configs overwrite org-wide configs with the
    same name.
    """
    from sqlalchemy import or_

    configs: dict[str, dict[str, Any]] = {}

    async with session_factory() as db:
        result = await db.execute(
            select(McpServer)
            .where(McpServer.org_id == org_id)
            .where(or_(McpServer.user_id.is_(None), McpServer.user_id == user_id))
            .where(McpServer.enabled.is_(True))
            .order_by(McpServer.user_id.asc().nulls_first())
        )
        for row in result.scalars().all():
            # User-specific rows come after org-wide (nulls first),
            # so they naturally overwrite.
            configs[row.name] = _row_to_config(row)

    return configs


def _row_to_config(row: McpServer) -> dict[str, Any]:
    """Convert a DB row to a config dict."""
    return {
        "transport": row.transport,
        "command": row.command,
        "args": list(row.args) if row.args else [],
        "url": row.url,
        "env": dict(row.env) if row.env else {},
        "timeout": row.timeout,
        "credential_refs": list(row.credential_refs) if row.credential_refs else [],
    }


async def _resolve_credentials(
    config: dict[str, Any],
    credential_refs: list[Any],
    vault: CredentialVault,
    org_id: UUID,
    user_id: UUID,
) -> None:
    """Resolve credential references and inject into the config.

    For each ref, tries the user-scoped credential first, then falls
    back to the org-scoped credential.
    """
    transport = config.get("transport", "stdio")
    env = config.setdefault("env", {})
    headers = config.setdefault("headers", {})

    for ref in credential_refs:
        if isinstance(ref, str):
            # Simple string: credential name maps to env var (stdio)
            # or Authorization header (http).
            name = ref
            value = await _retrieve_credential(vault, org_id, user_id, name)
            if value is None:
                logger.warning(
                    "Credential %r not found for org %s", name, org_id,
                )
                continue

            if transport == "stdio":
                env[name] = value
            else:
                headers["Authorization"] = f"Bearer {value}"

        elif isinstance(ref, dict):
            name = ref.get("name", "")
            if not name:
                continue

            value = await _retrieve_credential(vault, org_id, user_id, name)
            if value is None:
                logger.warning(
                    "Credential %r not found for org %s", name, org_id,
                )
                continue

            if "env" in ref:
                env[ref["env"]] = value
            elif "header" in ref:
                prefix = ref.get("prefix", "")
                headers[ref["header"]] = f"{prefix}{value}"
            else:
                # Default: inject as env var with the credential name.
                env[name] = value


async def _retrieve_credential(
    vault: CredentialVault,
    org_id: UUID,
    user_id: UUID,
    name: str,
) -> str | None:
    """Retrieve a credential, trying user-scoped first then org-scoped."""
    # User-scoped first.
    value = await vault.retrieve(org_id, name, user_id=user_id)
    if value is not None:
        return value

    # Fall back to org-scoped.
    return await vault.retrieve(org_id, name, user_id=None)
