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
import re
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
    allowed_ids: frozenset[str],
    is_service_account: bool = False,
    agent_id: str | None = None,
    session_id: str | None = None,
    platform_client: Any | None = None,
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
    # Per-agent allow-list scopes WHICH servers load; user_id still
    # drives per-caller credential resolution below.
    merged = await _load_db_configs(session_factory, org_id, allowed_ids)

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

    # Composio Tool Router servers are minted per end-user by ops, after
    # (and independent of) vault credential resolution above.
    merged = await apply_composio_minting(
        merged, platform_client=platform_client, agent_id=agent_id,
        user_id=user_id, session_id=session_id,
    )

    return merged


COMPOSIO_SERVER_NAME = "composio-tool-router"

# Strip the Composio brand from anything the model reads (tool names +
# descriptions advertised to the LLM) so the agent never echoes it back to
# the end user. Three forms, in order:
#   1. ``COMPOSIO_`` tool-slug prefix (e.g. COMPOSIO_SEARCH_TOOLS) — also
#      rewrites the cross-references inside descriptions so they keep
#      pointing at the (now debranded) tool names.
#   2. ``composio_`` / ``composio-`` server-slug fragments (composio-tool-router).
#   3. The capitalised brand word in prose ("Composio connects 500+ apps…").
# Lowercase standalone ``composio`` is intentionally left alone so functional
# URLs (connect.composio.dev) in tool *results* are never corrupted.
_DEBRAND_PATTERNS = (
    (re.compile(r"COMPOSIO_"), ""),
    (re.compile(r"composio[_-]"), ""),
    (re.compile(r"\bComposio\b ?"), ""),
)


def debrand_composio_text(text: str) -> str:
    """Remove Composio branding from model-facing tool names / descriptions."""
    if not text:
        return text
    for pattern, repl in _DEBRAND_PATTERNS:
        text = pattern.sub(repl, text)
    return text


async def apply_composio_minting(
    configs: dict[str, dict[str, Any]],
    *,
    platform_client: Any | None,
    agent_id: str | None,
    user_id: Any,
    session_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Replace ``composio``-transport placeholders with one minted HTTP server.

    Composio Tool Router URLs + headers are minted per end-user by ops
    for this agent; they are NOT vault-backed, so this runs after
    credential resolution and the returned headers are used verbatim.
    The headers carry the Composio ``x-api-key`` — never log this dict.

    Any number of ``composio`` placeholder rows (the agent's assigned
    toolkits, already filtered to this agent by the caller) collapse into
    a single ``composio-tool-router`` server.  A mint failure or an
    unconfigured/empty result drops the placeholders and leaves the rest
    of the tool set intact — a Composio outage never breaks other tools.
    """
    composio_names = [
        name for name, cfg in configs.items()
        if str(cfg.get("transport", "")).lower() == "composio"
    ]
    if not composio_names:
        return configs

    merged = {n: c for n, c in configs.items() if n not in composio_names}

    if platform_client is None or not agent_id:
        logger.warning(
            "Composio placeholders present for agent %s but no platform "
            "client to mint a session; skipping Composio tools", agent_id,
        )
        return merged

    try:
        minted = await platform_client.mint_composio_session(
            str(agent_id), str(user_id),
            session_id=str(session_id) if session_id is not None else None,
        )
    except Exception:  # noqa: BLE001 — a Composio failure must not drop other tools
        logger.warning(
            "Failed to mint Composio session for agent %s; serving other "
            "MCP tools without Composio", agent_id, exc_info=True,
        )
        return merged

    if not minted or not minted.get("url"):
        return merged

    merged[COMPOSIO_SERVER_NAME] = {
        "transport": str(minted.get("transport", "http") or "http"),
        "url": str(minted["url"]),
        "headers": dict(minted.get("headers", {}) or {}),
        "command": None,
        "args": [],
        "env": {},
        "timeout": minted.get("timeout", 120),
        "credential_refs": [],
    }
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
    allowed_ids: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Load the agent's attached MCP server configs from the database.

    Strict per-agent scoping: only servers whose id is in *allowed_ids*
    (the agent's ``mcp_server_ids`` from its runtime config) are
    returned.  An empty allow-list short-circuits to ``{}`` — an agent
    with no attached servers gets no MCP tools.  ``org_id`` is retained
    as a defense-in-depth bound.

    When two attached rows share a ``name`` (an org row + a user row),
    the user-specific row wins (``nulls_first`` ordering means it is
    applied last); ties beyond that resolve by stable ``id`` order.
    """
    if not allowed_ids:
        return {}

    # ``UUID`` is imported at module scope (loader.py:34). ctx.mcp_server_ids
    # are strings; McpServer.id is a UUID column.  A malformed id is a
    # corrupt runtime config, not a reason to 500 the whole discovery —
    # skip it (and log) and scope to the valid ones.
    id_values: list[UUID] = []
    for raw in allowed_ids:
        try:
            id_values.append(UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "Ignoring malformed MCP server id in agent allow-list: %r",
                raw,
            )
    if not id_values:
        return {}

    configs: dict[str, dict[str, Any]] = {}

    async with session_factory() as db:
        result = await db.execute(
            select(McpServer)
            .where(McpServer.org_id == org_id)
            .where(McpServer.id.in_(id_values))
            .where(McpServer.enabled.is_(True))
            .order_by(
                McpServer.user_id.asc().nulls_first(),
                McpServer.id.asc(),
            )
        )
        for row in result.scalars().all():
            # Deterministic precedence: org-wide (nulls first) then
            # user-specific overwrite by name, then stable id order.
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
                is_service_account=is_service_account,
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
                is_service_account=is_service_account,
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
    *,
    is_service_account: bool = False,
) -> tuple[str | None, str]:
    """Retrieve a credential; returns ``(value, scope)``.

    ``scope`` is ``"user"`` when a user's personal vault supplied the value,
    ``"service_account"`` when it came from an agent service-account vault,
    ``"org"`` when it came from the org-wide vault, and ``"missing"`` when
    none had the credential.  When *is_service_account* is set, ``user_id``
    carries the service-account principal id.

    Routes lookups through :meth:`CredentialVault.resolve_ref` so the single
    canonical per-session entry point is used.
    """
    ref = f"vault://{name}"
    if is_service_account:
        value = await vault.resolve_ref(
            ref,
            org_id=org_id,
            service_account_id=user_id,
        )
        if value is not None:
            return value, "service_account"
    else:
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
