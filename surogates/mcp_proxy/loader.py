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

from surogates.audit import AuditStore, AuditType, credential_access_event
from surogates.db.models import McpServer
from surogates.tenant.credentials import CredentialVault

logger = logging.getLogger(__name__)


async def load_mcp_configs(
    org_id: UUID,
    user_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    vault: CredentialVault,
    audit_store: AuditStore | None = None,
    *,
    is_service_account: bool = False,
    agent_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load, merge, and credential-resolve MCP server configs.

    Returns a dict of ``{server_name: config_dict}`` ready for
    ``MCPServerTask`` / ``discover_mcp_tools()``.

    The on-disk ConfigMap fallback is retired.
    The MCP server registry is now exclusively DB-backed — admins
    use the surogate-ops UI which writes to the ``mcp_servers``
    table; the proxy reads it here.  Merge precedence is just
    org-wide < user-specific.  Disabled servers are excluded.

    When *audit_store* is provided every credential lookup emits a
    ``credential.access`` entry to the tenant audit log.  When it is
    ``None`` resolution proceeds silently (useful in tests and local
    dev).
    """
    # DB configs (org-wide + user-specific).
    merged = await _load_db_configs(session_factory, org_id, user_id)

    # 4. Resolve credential_refs in parallel across all servers.
    resolve_tasks = []
    for server_name, config in merged.items():
        credential_refs = config.pop("credential_refs", [])
        if credential_refs:
            resolve_tasks.append(
                _resolve_credentials_safe(
                    server_name, config, credential_refs, vault, org_id,
                    user_id, audit_store,
                    is_service_account=is_service_account,
                    agent_id=agent_id,
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
    audit_store: AuditStore | None,
    *,
    is_service_account: bool = False,
    agent_id: str | None = None,
) -> None:
    """Wrapper that catches and logs credential resolution failures."""
    try:
        await _resolve_credentials(
            config, credential_refs, vault, org_id, user_id,
            server_name, audit_store,
            is_service_account=is_service_account,
            agent_id=agent_id,
        )
    except Exception:
        logger.exception(
            "Failed to resolve credentials for MCP server %s", server_name,
        )


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
    server_name: str,
    audit_store: AuditStore | None,
    *,
    is_service_account: bool = False,
    agent_id: str | None = None,
) -> None:
    """Resolve credential references and inject into the config.

    For each ref, tries the user-scoped credential first, then falls
    back to the org-scoped credential.  Each lookup is recorded in the
    tenant audit log when *audit_store* is provided.
    """
    transport = config.get("transport", "stdio")
    env = config.setdefault("env", {})
    headers = config.setdefault("headers", {})
    consumer = f"mcp_server:{server_name}"
    # See note in pool._scan_and_record: service-account principals must
    # not populate ``audit_log.user_id`` because that column FKs to
    # ``users.id``.
    audit_user_id: UUID | None = None if is_service_account else user_id

    for ref in credential_refs:
        if isinstance(ref, str):
            # Simple string: credential name maps to env var (stdio)
            # or Authorization header (http).
            name = ref
            value, scope = await _retrieve_credential(
                vault, org_id, user_id, name,
            )
            await _emit_credential_access(
                audit_store, org_id, audit_user_id, name, consumer, scope,
                agent_id=agent_id,
            )
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

            value, scope = await _retrieve_credential(
                vault, org_id, user_id, name,
            )
            await _emit_credential_access(
                audit_store, org_id, audit_user_id, name, consumer, scope,
                agent_id=agent_id,
            )
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
) -> tuple[str | None, str]:
    """Retrieve a credential; returns ``(value, scope)``.

    ``scope`` is ``"user"`` when the user's personal vault supplied the
    value, ``"org"`` when it came from the org-wide vault, and
    ``"missing"`` when neither had the credential.

    Routes both lookups through
    :meth:`CredentialVault.resolve_ref` so the single canonical
    per-session entry point is used;
    """
    ref = f"vault://{name}"
    # User-scoped first.
    value = await vault.resolve_ref(ref, org_id=org_id, user_id=user_id)
    if value is not None:
        return value, "user"

    # Fall back to org-scoped.
    value = await vault.resolve_ref(ref, org_id=org_id, user_id=None)
    if value is not None:
        return value, "org"

    return None, "missing"


async def _emit_credential_access(
    audit_store: AuditStore | None,
    org_id: UUID,
    user_id: UUID | None,
    name: str,
    consumer: str,
    scope: str,
    *,
    agent_id: str | None,
) -> None:
    """Record a credential.access entry when the audit store is wired.

    ``agent_id`` is required (keyword-only) instead of being silently
    set to ``None`` at the ``audit_store.emit`` call site.  Callers
    thread it through from the proxy routes via ``ctx.agent_id``
    resolved by ``agent_runtime_context_dep``.  Callers without an
    agent context (e.g. platform-wide scans) pass ``None`` explicitly
    so the row persists with a NULL agent_id (the column is nullable
    by design).
    """
    if audit_store is None:
        return
    await audit_store.emit(
        org_id=org_id,
        agent_id=agent_id,
        user_id=user_id,
        type=AuditType.CREDENTIAL_ACCESS,
        data=credential_access_event(
            name, consumer=consumer, scope=scope, found=scope != "missing",
        ),
    )
