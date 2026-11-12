"""MCP connection pool manager.

Manages ``MCPServerTask`` instances keyed by ``(org_id, user_id,
server_name)``.  Connections are created lazily on first tool-list or
tool-call request and evicted after an idle timeout.

The pool reuses the MCP client's module-level background event loop
and ``_servers`` dict, but prefixes each server name with the tenant
context to prevent collisions across organisations and users.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from surogates.audit import (
    AuditStore,
    AuditType,
    mcp_scan_event,
    rug_pull_event,
)
from surogates.governance.mcp_scanner import MCPGovernance, _fingerprint
from surogates.mcp_proxy.loader import (
    COMPOSIO_SERVER_NAME,
    debrand_composio_text,
)
from surogates.tools.mcp.client import (
    _MCP_AVAILABLE,
    _UTILITY_OFFER,
    _lock,
    _servers,
    _sanitize_error,
    call_utility,
    discover_mcp_tools,
    sanitize_mcp_name_component,
)
from surogates.tools.mcp.names import fit_tool_name, mcp_tool_name

logger = logging.getLogger(__name__)

#: How many tenants' approved tool fingerprints the pool keeps (the least
#: recently used without a live connection dropped first).  Tenants with a
#: live connection are never dropped, so while more than this many are
#: connected the store holds one set per live connection.
_MAX_TENANT_APPROVALS = 10_000
#: How many tools of one server (one identity) a tenant may have approved,
#: and how many in all: a new tool past either is left out, and old
#: approvals are never dropped to make room, so a server that floods new
#: names starves only itself.
_MAX_APPROVALS_PER_SERVER = 1_000
_MAX_APPROVALS_PER_TENANT = 20_000


class _ApprovalRoom:
    """How many more tools a tenant may have approved, per server and in all,
    counted once per connection."""

    def __init__(self, approvals: dict[str, str]) -> None:
        self._approvals = approvals
        self._per_server: dict[str, int] = {}

    def take(self, server_prefix: str) -> bool:
        """Room for one more approval of the server named by *server_prefix*."""
        if server_prefix not in self._per_server:
            self._per_server[server_prefix] = sum(1 for k in self._approvals if k.startswith(server_prefix))
        if (
            self._per_server[server_prefix] >= _MAX_APPROVALS_PER_SERVER
            or len(self._approvals) >= _MAX_APPROVALS_PER_TENANT
        ):
            return False
        self._per_server[server_prefix] += 1
        return True


def _server_identity(config: dict[str, Any] | None) -> str:
    """What a server is, for its approvals: a digest of its transport, URL,
    command and arguments.  Credentials (headers, env) are left out, so a
    rotated token keeps the approvals; an admin who repoints the server
    gets it approved anew."""
    if not config:
        return ""
    ident = {k: config.get(k) for k in ("transport", "url", "command", "args")}
    canonical = json.dumps(ident, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _tenant_prefix(org_id: UUID, user_id: UUID, agent_id: str) -> str:
    """Build the server-name prefix that makes module-level entries unique.

    Includes ``agent_id`` so two agents under the same ``(org, user)``
    never share a long-lived upstream connection in the module-level
    ``_servers`` dict — each agent connects only to its attached servers.
    """
    return f"{org_id}_{user_id}_{agent_id}"


def _prefixed_name(
    org_id: UUID, user_id: UUID, agent_id: str, server_name: str,
) -> str:
    """Full agent-scoped server name used as key in the global _servers dict."""
    return f"{_tenant_prefix(org_id, user_id, agent_id)}__{server_name}"


def _flatten_openai_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI-format tool schema to a flat one.

    The ``ToolRegistry.get_schemas`` output is shaped for
    ``chat.completions.create(tools=...)``::

        {"type": "function", "function": {"name", "description", "parameters"}}

    The proxy's own protocol and the in-cluster bookkeeping work on the
    flat ``{name, description, parameters}`` shape.  Already-flat inputs
    are returned unchanged so this helper is safe to call defensively.
    """
    fn = schema.get("function")
    if isinstance(fn, dict):
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
    return schema


@dataclass
class PoolEntry:
    """Tracks a tenant's MCP server set and its last-access time.

    ``governance`` is the tenant-scoped :class:`MCPGovernance` instance
    that checks this tenant's tools against the fingerprints approved at
    its earlier connections (:meth:`ConnectionPool._tenant_approvals`).
    One per ``(org_id, user_id, agent_id)`` so fingerprints never leak
    across tenants — a rug-pull in tenant A's server must not suppress a
    scan in tenant B's differently-configured server with the same name.
    """

    org_id: UUID
    user_id: UUID
    agent_id: str
    server_names: list[str]  # original (unprefixed) server names
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    # Reverse index: clean tool name -> (prefixed server key, original MCP tool name)
    tool_index: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Per-server config (unresolved -- credentials
    # have already been injected into ``env`` during
    # ``load_mcp_configs`` -> ``_resolve_credentials``).  The route
    # looks up ``server_configs[original_server_name]`` to spawn an
    # ``MCPCallSandbox`` per call.
    server_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    sanitized_prefix: str = ""
    last_used: float = field(default_factory=time.monotonic)
    governance: MCPGovernance | None = None
    # The MCP utility tools among the clean names: name -> what it runs
    # (``list_resources``, ``read_resource``, ``list_prompts``,
    # ``get_prompt``), called as that MCP method, never as ``tools/call``.
    utility_index: dict[str, str] = field(default_factory=dict)


class ConnectionPool:
    """Manages tenant-scoped MCP server connections.

    Parameters
    ----------
    idle_timeout:
        Seconds of inactivity before a tenant's connections are evicted.
    max_per_org:
        Maximum number of concurrent MCP connections per organisation.
    governance_enabled:
        When True, every advertised MCP tool is scanned for prompt
        injection, hidden instructions, invisible Unicode, etc. — and
        tracked for rug-pull mutations on reconnect.  The fingerprints
        approved at a tenant's first connection outlive its pool entry
        (``_approvals``), so a later connection whose tool definitions
        changed is caught, audited and left without those tools.  They are
        kept per server identity (transport, URL, command, arguments): a
        server an admin repoints is approved anew.  They live in this
        process only; a restart, or another replica, approves anew.
        Unsafe tools are filtered out of the advertised schema set.
    audit_store:
        Optional :class:`AuditStore` that receives
        ``policy.mcp_scan`` and ``policy.rug_pull`` entries per scanned
        tool.  Has no effect when *governance_enabled* is False.
    """

    def __init__(
        self,
        idle_timeout: int = 300,
        max_per_org: int = 50,
        *,
        governance_enabled: bool = False,
        audit_store: AuditStore | None = None,
    ) -> None:
        self._idle_timeout = idle_timeout
        self._max_per_org = max_per_org
        self._entries: dict[tuple[UUID, UUID, str], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID, str], asyncio.Lock] = {}
        # Background agent-eviction tasks. Held so they are not garbage-
        # collected mid-flight (CPython only keeps weak refs to tasks);
        # each removes itself via a done-callback.
        self._invalidation_tasks: set[asyncio.Task] = set()
        self._eviction_task: asyncio.Task | None = None
        self._governance_enabled = governance_enabled
        self._audit_store = audit_store
        # Per tenant, the fingerprints of the tools approved at its first
        # connection: ``server.tool`` -> SHA-256.  Kept when the tenant's
        # entry is evicted, so its next connection is checked against them.
        self._approvals: OrderedDict[tuple[UUID, UUID, str], dict[str, str]] = OrderedDict()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_eviction_loop(self) -> None:
        """Start the background idle-eviction loop."""
        if self._eviction_task is None or self._eviction_task.done():
            self._eviction_task = asyncio.create_task(
                self._evict_idle(), name="mcp-pool-eviction",
            )

    async def shutdown(self) -> None:
        """Shut down all connections and stop the eviction loop."""
        if self._eviction_task and not self._eviction_task.done():
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass

        # Drain any in-flight agent-invalidation teardowns before we
        # clear shared state out from under them.
        for task in list(self._invalidation_tasks):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._invalidation_tasks.clear()

        for entry in list(self._entries.values()):
            await self._disconnect_entry(entry)
        self._entries.clear()
        self._locks.clear()

    # ------------------------------------------------------------------
    # Cache query
    # ------------------------------------------------------------------

    def get_cached_schemas(
        self, org_id: UUID, user_id: UUID, agent_id: str,
    ) -> list[dict[str, Any]] | None:
        """Return cached tool schemas if the agent is connected, else None."""
        entry = self._entries.get((org_id, user_id, agent_id))
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        self._touch_approvals((org_id, user_id, agent_id))
        return entry.tool_schemas

    def visible_schemas(
        self, org_id: UUID, user_id: UUID, agent_id: str, schemas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """*schemas* without the resource / prompt tools of servers that have no resources / prompts."""
        entry = self._entries.get((org_id, user_id, agent_id))
        if entry is None or not entry.utility_index:
            return schemas
        # Read from the live connections: a reconnect reads a server's offers anew.
        with _lock:
            idle = {
                name for name, utility in entry.utility_index.items()
                if not self._offers(entry.tool_index.get(name, ("", ""))[0], utility)
            }
        return [s for s in schemas if s.get("name") not in idle]

    @staticmethod
    def _offers(server_key: str, utility: str) -> bool:
        """Whether the connected server *server_key* offers what *utility* needs (call under ``_lock``)."""
        server = _servers.get(server_key)
        return bool((getattr(server, "offers", None) or {}).get(_UTILITY_OFFER[utility]))

    def utility_of(
        self, org_id: UUID, user_id: UUID, agent_id: str, tool_name: str,
    ) -> str | None:
        """The MCP utility a clean tool name runs, or None for a server's own tool."""
        entry = self._entries.get((org_id, user_id, agent_id))
        return None if entry is None else entry.utility_index.get(tool_name)

    def resolve_call_target(
        self, org_id: UUID, user_id: UUID, agent_id: str, tool_name: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve a clean tool name to the per-call subprocess parameters.

        Returns
        ``(original_server_name, original_tool_name, server_config)``
        if the tenant is connected and the tool is in scope; ``None``
        otherwise.  The route uses the returned ``server_config`` to
        spawn a fresh :class:`MCPCallSandbox` per call instead of
        reaching into the module-level ``_servers`` dict for the
        long-lived session.
        """
        entry = self._entries.get((org_id, user_id, agent_id))
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        self._touch_approvals((org_id, user_id, agent_id))
        routing = entry.tool_index.get(tool_name)
        if routing is None:
            return None
        server_key, original_tool = routing
        # Strip the agent prefix from the server key to recover the
        # config dict key (server_configs is indexed by the original
        # unprefixed name).
        expected_prefix = f"{_tenant_prefix(org_id, user_id, agent_id)}__"
        if server_key.startswith(expected_prefix):
            original_server = server_key[len(expected_prefix):]
        else:
            original_server = server_key
        config = entry.server_configs.get(original_server)
        if config is None:
            return None
        return original_server, original_tool, config

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def ensure_connected(
        self,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        configs: dict[str, dict[str, Any]],
        *,
        is_service_account: bool = False,
    ) -> list[dict[str, Any]]:
        """Ensure MCP servers for this tenant are connected and return
        their tool schemas.

        Idempotent — if already connected, returns cached schemas.

        ``is_service_account`` marks ``user_id`` as a service-account id
        rather than a real ``users.id``; downstream audit writes use
        ``user_id=None`` in that case to avoid FK violations.
        """
        key = (org_id, user_id, agent_id)
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
                self._touch_approvals(key)
                return entry.tool_schemas

            # Prefix each server name for tenant isolation.  The resource /
            # prompt utility tools are registered for every server here; a
            # list leaves out those whose server offers nothing of that kind,
            # read from the live connection (``visible_schemas``).
            prefixed: dict[str, dict[str, Any]] = {}
            original_names: list[str] = []
            for name, cfg in configs.items():
                pname = _prefixed_name(org_id, user_id, agent_id, name)
                prefixed[pname] = {**cfg, "tools": {**(cfg.get("tools") or {}), "utilities": "all"}}
                original_names.append(name)

            from surogates.tools.registry import ToolRegistry

            temp_registry = ToolRegistry()
            registered = await asyncio.to_thread(
                discover_mcp_tools,
                servers=prefixed,
                registry=temp_registry,
            )

            # ``ToolRegistry.get_schemas`` emits OpenAI's nested
            # ``{"type":"function","function":{"name",...}}`` shape; the
            # rest of this method (and the ``/mcp/v1/tools/list`` route
            # contract) work on a flat ``{name,description,parameters}``
            # shape, so unwrap once here. Skipping this step is what
            # makes the proxy advertise empty-named tools and the worker
            # silently register zero MCP tools.
            schemas = [
                _flatten_openai_schema(s)
                for s in temp_registry.get_schemas(names=set(registered))
            ]
            for s in schemas:
                tool = temp_registry.get(s["name"])
                if tool is not None and tool.schema.annotations:
                    s["annotations"] = tool.schema.annotations

            # Build clean schemas and reverse tool index.
            tenant_pfx = sanitize_mcp_name_component(
                _tenant_prefix(org_id, user_id, agent_id),
            )
            clean_schemas: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            tool_index: dict[str, tuple[str, str]] = {}
            utility_index: dict[str, str] = {}

            # Per-tenant MCPGovernance over the tenant's approved
            # fingerprints, keyed by (server, tool), never shared with
            # other tenants even when server names collide.
            tenant_governance: MCPGovernance | None = None
            approval_room: _ApprovalRoom | None = None
            if self._governance_enabled:
                approvals = self._tenant_approvals(key)
                tenant_governance = MCPGovernance(fingerprints=approvals)
                approval_room = _ApprovalRoom(approvals)

            for schema in schemas:
                clean = dict(schema)
                # The proxy's own name for the tool, under the tenant-scoped
                # server key (``mcp__{tenant_prefix}__{server}__{tool}``,
                # shortened: it is never parsed back).
                raw_name = clean.get("name", "")
                clean_name = raw_name

                # Build reverse index for O(1) tool call routing.
                # Find which prefixed server owns this tool, and the MCP
                # name the tool is called by there.
                owning_server_key: str | None = None
                original_tool: str = ""
                utility: str | None = None
                with _lock:
                    for server_key, server in _servers.items():
                        if raw_name in (getattr(server, "_registered_tool_names", None) or ()):
                            owning_server_key = server_key
                            original_tool = (getattr(server, "_tool_names", None) or {}).get(raw_name, raw_name)
                            utility = (getattr(server, "_utility_names", None) or {}).get(raw_name)
                            break
                if owning_server_key is not None:
                    expected = f"{_tenant_prefix(org_id, user_id, agent_id)}__"
                    plain = owning_server_key[len(expected):] if owning_server_key.startswith(expected) else owning_server_key
                    # The name the agent knows: ``mcp__{server}__{tool}``,
                    # shortened when an engine would refuse it.
                    clean_name = mcp_tool_name(plain, original_tool)
                    clean["server"] = plain
                    # A description the harness wrote (a utility's, or the
                    # stand-in for a tool the server left undescribed) names
                    # the server by the name the agent knows, not by the
                    # tenant-scoped key with its org / user / agent ids.
                    clean["description"] = str(clean.get("description", "")).replace(owning_server_key, plain)
                clean["name"] = clean_name

                # Governance scan — run before adding the tool to the
                # advertised schema set so unsafe tools never reach the
                # agent.  Skipped when governance is disabled on the pool.
                if tenant_governance is not None:
                    original_server = ""
                    if owning_server_key is not None:
                        # Strip the agent prefix back off the server key.
                        expected_prefix = f"{_tenant_prefix(org_id, user_id, agent_id)}__"
                        if owning_server_key.startswith(expected_prefix):
                            original_server = owning_server_key[len(expected_prefix):]
                        else:
                            original_server = owning_server_key

                    # The Composio Tool Router is a first-party, platform-minted
                    # server (we create the session against backend.composio.dev
                    # with our own API key). Its meta-tool descriptions
                    # legitimately use imperative guidance ("you must …") and
                    # remote-execution fields ("command"/"code_to_execute") that
                    # trip the third-party tool-poisoning / description-injection
                    # heuristics. Exempt it from the scan-drop so
                    # COMPOSIO_SEARCH_TOOLS (and the bash/workbench executors)
                    # reach the agent — without SEARCH_TOOLS the router cannot
                    # discover or execute any app tool and is dead weight.
                    is_trusted_server = original_server == COMPOSIO_SERVER_NAME
                    if not is_trusted_server and not await self._scan_and_record(
                        governance=tenant_governance,
                        org_id=org_id,
                        user_id=user_id,
                        server_name=original_server,
                        tool_name=original_tool or clean_name,
                        schema=clean,
                        is_service_account=is_service_account,
                        server_identity=_server_identity(configs.get(original_server)),
                        approval_room=approval_room,
                    ):
                        continue  # unsafe or rug-pulled — exclude from advertised set

                    # Debrand the Composio router's advertised name + description
                    # so the model never reads (and then echoes) the brand. The
                    # reverse-route below keeps using the real upstream tool name
                    # (``original_tool``), so calls still reach Composio.
                    if is_trusted_server:
                        # Named from the debranded server and tool, so the
                        # name keeps its ``mcp__tool_router__`` prefix and is
                        # shortened (if at all) after debranding.
                        clean["name"] = fit_tool_name(
                            debrand_composio_text(sanitize_mcp_name_component(original_server)),
                            debrand_composio_text(sanitize_mcp_name_component(original_tool or clean_name)),
                        )
                        clean["description"] = debrand_composio_text(
                            clean.get("description", ""),
                        )
                        clean_name = clean["name"]

                if clean_name in seen_names:
                    # Two tools under one name (their names sanitize alike):
                    # the first keeps it, as in process.
                    logger.warning(
                        "MCP tool %s from server %s shares its name %s with an earlier tool -- skipped",
                        original_tool, owning_server_key, clean_name,
                    )
                    continue
                seen_names.add(clean_name)
                clean_schemas.append(clean)
                if owning_server_key is not None:
                    tool_index[clean_name] = (owning_server_key, original_tool)
                if utility is not None:
                    utility_index[clean_name] = utility

            entry = PoolEntry(
                org_id=org_id,
                user_id=user_id,
                agent_id=agent_id,
                server_names=original_names,
                tool_schemas=clean_schemas,
                tool_index=tool_index,
                server_configs={
                    name: dict(cfg) for name, cfg in configs.items()
                },
                sanitized_prefix=tenant_pfx,
                governance=tenant_governance,
                utility_index=utility_index,
            )
            self._entries[key] = entry

            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s agent=%s "
                "(%d tools)",
                len(original_names), org_id, user_id, agent_id,
                len(clean_schemas),
            )
            return clean_schemas

    # ------------------------------------------------------------------
    # Governance scan helper
    # ------------------------------------------------------------------

    def _touch_approvals(self, key: tuple[UUID, UUID, str]) -> None:
        """Mark tenant *key*'s approvals as just used."""
        if key in self._approvals:
            self._approvals.move_to_end(key)

    def _tenant_approvals(self, key: tuple[UUID, UUID, str]) -> dict[str, str]:
        """The fingerprints approved for tenant *key*, kept across its connections.

        At most ``_MAX_TENANT_APPROVALS`` tenants are kept: past that, the
        least recently used tenant without a live connection is dropped
        first, and is approved anew at its next connection.  A tenant with
        a live connection is never dropped.
        """
        approvals = self._approvals.get(key)
        if approvals is None:
            approvals = self._approvals[key] = {}
            excess = len(self._approvals) - _MAX_TENANT_APPROVALS
            if excess > 0:
                for old in [k for k in self._approvals if k != key and k not in self._entries][:excess]:
                    del self._approvals[old]
        else:
            self._approvals.move_to_end(key)
        return approvals

    async def _scan_and_record(
        self,
        *,
        governance: MCPGovernance,
        org_id: UUID,
        user_id: UUID,
        server_name: str,
        tool_name: str,
        schema: dict[str, Any],
        is_service_account: bool = False,
        server_identity: str = "",
        approval_room: _ApprovalRoom | None = None,
    ) -> bool:
        """Scan *schema* for safety + rug-pull; return True if tool is safe.

        *server_identity* names what the server is (:func:`_server_identity`):
        approvals are kept per identity, so a server an admin repoints (a new
        URL, command or arguments) is approved anew, not held against the
        definitions of the server it was.

        *governance* is tenant-scoped (one instance per :class:`PoolEntry`,
        over the tenant's approvals) so fingerprints never leak between
        tenants.  Emits a
        ``policy.mcp_scan`` row for every scan and an additional
        ``policy.rug_pull`` row when a previously-registered tool's
        fingerprint has changed.  Unsafe or rug-pulled tools return
        ``False`` and are excluded from the advertised schema set.
        """
        # MCPGovernance expects MCP-style tool_def with ``inputSchema``;
        # schemas here are OpenAI-shaped with ``parameters``.  Adapt.
        tool_def = {
            "name": tool_name,
            "description": schema.get("description", ""),
            "inputSchema": schema.get("parameters") or {},
            "_server_name": server_name,
        }
        server_prefix = f"{server_name}@{server_identity}." if server_identity else f"{server_name}."
        qualified_name = f"{server_prefix}{tool_name}"

        # The ``audit_log.user_id`` column FKs to ``users.id``; for
        # service-account sessions the JWT carries a ``service_accounts.id``
        # instead, so persist NULL there to avoid FK violations while
        # still scoping the row by ``org_id``.
        audit_user_id: UUID | None = None if is_service_account else user_id

        # Rug-pull check — against the definition approved at one of this
        # tenant's earlier connections to the same server.
        if governance.has_fingerprint(qualified_name):
            if not governance.check_rug_pull(qualified_name, tool_def):
                if self._audit_store is not None:
                    await self._audit_store.emit(
                        org_id=org_id,
                        # MCP scans are org-scoped (server definitions
                        # live at the org level); no single agent_id
                        # to attribute.
                        agent_id=None,
                        user_id=audit_user_id,
                        type=AuditType.POLICY_RUG_PULL,
                        data=rug_pull_event(
                            server_name, tool_name,
                            previous_fingerprint=(
                                governance.get_fingerprint(qualified_name) or ""
                            ),
                            current_fingerprint=_fingerprint(tool_def),
                        ),
                    )
                logger.warning(
                    "Rug-pull detected for %s from server %s — tool excluded",
                    tool_name, server_name,
                )
                return False

        result = governance.scan_tool(tool_def)

        if self._audit_store is not None:
            await self._audit_store.emit(
                org_id=org_id,
                agent_id=None,  # org-scoped scan — see rug_pull emit above
                user_id=audit_user_id,
                type=AuditType.POLICY_MCP_SCAN,
                data=mcp_scan_event(
                    server_name, tool_name,
                    safe=result.safe,
                    threats=list(result.threats),
                    severity=result.severity,
                    advisories=list(result.advisories),
                ),
            )

        if not result.safe:
            logger.warning(
                "Unsafe MCP tool %s from server %s [%s]: %s",
                tool_name, server_name, result.severity,
                "; ".join(result.threats),
            )
            return False

        if result.advisories:
            logger.warning(
                "MCP tool %s from server %s kept with scan advisories (wording heuristics, not dropped): %s",
                tool_name, server_name, "; ".join(result.advisories),
            )

        if not governance.has_fingerprint(qualified_name) and not (
            approval_room.take(server_prefix) if approval_room is not None
            else governance.fingerprint_count() < _MAX_APPROVALS_PER_TENANT
        ):
            # Old approvals are never dropped to make room: a server could
            # otherwise flood new names until its own approvals went.
            logger.warning(
                "MCP tool %s from server %s not approved: no room for more approvals "
                "(%d per server, %d per tenant)",
                tool_name, server_name, _MAX_APPROVALS_PER_SERVER, _MAX_APPROVALS_PER_TENANT,
            )
            return False
        governance.register_fingerprint(qualified_name, tool_def)
        return True

    # ------------------------------------------------------------------
    # Tool calling
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        org_id: UUID,
        user_id: UUID,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Call an MCP tool and return the result string.

        Uses the reverse tool index for O(1) server lookup. ``meta``,
        when present, is forwarded to the upstream MCP server via the
        ``_meta`` channel so platform servers can scope calls by chat
        user / project without an extra round-trip.
        """
        key = (org_id, user_id, agent_id)
        entry = self._entries.get(key)
        if entry is None:
            return json.dumps({"error": "No MCP connections for this tenant."})

        entry.last_used = time.monotonic()

        # O(1) lookup via reverse index.
        routing = entry.tool_index.get(tool_name)
        if routing is None:
            return json.dumps({"error": f"Tool '{tool_name}' not found."})

        server_key, original_tool = routing

        # Read only the specific server under the lock.
        with _lock:
            server = _servers.get(server_key)

        if server is None or server.session is None:
            return json.dumps({
                "error": f"MCP server for tool '{tool_name}' is not connected.",
            })

        utility = entry.utility_index.get(tool_name)
        if utility is not None:
            return await self._execute_utility_call(server, utility, arguments)
        return await self._execute_tool_call(
            server, original_tool, arguments, meta=meta,
        )

    @staticmethod
    async def _execute_utility_call(server: Any, utility: str, arguments: dict[str, Any]) -> str:
        """Run an MCP utility (resources / prompts) on the server's long-lived session.

        As its MCP method (``resources/read``, ``prompts/get``, ...): a
        ``tools/call`` of ``read_resource`` reaches no server's resources.
        Bridged to the MCP loop as :meth:`_execute_tool_call` is.
        """
        from surogates.tools.mcp import client as _mcp_client_module

        try:
            coro = call_utility(server.session, utility, arguments)
            loop = _mcp_client_module._mcp_loop  # type: ignore[attr-defined]
            if loop is None or not loop.is_running():
                return await coro
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return await asyncio.wrap_future(future)
            except BaseException:
                future.cancel()
                raise
        except Exception as exc:
            return json.dumps({
                "error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}"),
            })

    @staticmethod
    async def _execute_tool_call(
        server: Any,
        tool_name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool call on the MCP server session.

        The session and its read/write streams are bound to the
        dedicated ``_mcp_loop`` thread created by
        :mod:`surogates.tools.mcp.client`. Awaiting ``call_tool``
        from the FastAPI main loop here would interact with anyio
        streams owned by a different loop, which Python's asyncio
        bridges via slow polling — measured at ~15 s per call on the
        copilot's read tools in PROD. Bridge through
        :func:`asyncio.run_coroutine_threadsafe` so the coroutine
        runs on the loop the streams were created on, then await the
        wrapped future on the current loop.
        """
        from surogates.tools.mcp import client as _mcp_client_module

        try:
            loop = _mcp_client_module._mcp_loop  # type: ignore[attr-defined]
            if loop is None or not loop.is_running():
                # No dedicated MCP loop running (unit tests build sessions
                # inline on the test loop); call directly. Cross-loop
                # interaction would be slow but functional.
                if meta:
                    result = await server.session.call_tool(
                        tool_name, arguments=arguments, meta=meta,
                    )
                else:
                    result = await server.session.call_tool(
                        tool_name, arguments=arguments,
                    )
            else:
                if meta:
                    coro = server.session.call_tool(
                        tool_name, arguments=arguments, meta=meta,
                    )
                else:
                    coro = server.session.call_tool(
                        tool_name, arguments=arguments,
                    )
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                try:
                    result = await asyncio.wrap_future(future)
                except BaseException:
                    future.cancel()
                    raise

            if result.isError:
                error_text = ""
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({
                    "error": _sanitize_error(
                        error_text or "MCP tool returned an error",
                    ),
                })

            parts: list[str] = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(f"[binary: {getattr(block, 'mimeType', 'unknown')}]")

            output = "\n".join(parts)
            return _sanitize_error(output)

        except asyncio.TimeoutError:
            return json.dumps({
                "error": f"MCP tool call '{tool_name}' timed out.",
            })
        except Exception as exc:
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {exc}",
                ),
            })

    # ------------------------------------------------------------------
    # Idle eviction
    # ------------------------------------------------------------------

    async def _disconnect_entry(self, entry: PoolEntry) -> None:
        """Remove all tenant-scoped servers from the module-level dict."""
        servers_to_shutdown = []
        with _lock:
            for name in entry.server_names:
                prefixed = _prefixed_name(
                    entry.org_id, entry.user_id, entry.agent_id, name,
                )
                server = _servers.pop(prefixed, None)
                if server is not None:
                    servers_to_shutdown.append(server)

        # ``server.shutdown()`` awaits ``self._task``, which was created on
        # the dedicated ``_mcp_loop`` thread (HTTP/stdio sessions and their
        # streams are bound to it).  Eviction / invalidation runs on a
        # different loop, so awaiting the task directly raises "got Future
        # attached to a different loop".  Bridge through ``_mcp_loop`` exactly
        # as ``_execute_tool_call`` does for calls.
        from surogates.tools.mcp import client as _mcp_client_module

        for server in servers_to_shutdown:
            try:
                loop = _mcp_client_module._mcp_loop  # type: ignore[attr-defined]
                if loop is None or not loop.is_running():
                    # No dedicated loop (unit tests build sessions inline on
                    # the test loop); shut down directly.
                    await server.shutdown()
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        server.shutdown(), loop,
                    )
                    await asyncio.wrap_future(future)
            except Exception:
                logger.debug(
                    "Error shutting down MCP server %s", server.name,
                    exc_info=True,
                )

    def invalidate_agent(self, agent_id: str) -> None:
        """Immediately evict every pool entry for *agent_id*.

        Called from the Redis invalidator when the agent's MCP
        attachments or runtime config change, so a detached server stops
        being callable at once rather than at the idle-eviction TTL.
        Synchronous removal from ``_entries`` is the security boundary;
        the upstream-connection teardown is best-effort cleanup
        scheduled on the running loop.
        """
        keys = [k for k in self._entries if k[2] == agent_id]
        # The approvals stay: an invalidation follows any edit of the agent,
        # and the next connection is held against them like any other.  A
        # server an admin repoints has a new identity (``_server_identity``)
        # and is approved anew.
        entries: list[PoolEntry] = []
        for key in keys:
            entry = self._entries.pop(key, None)
            self._locks.pop(key, None)
            if entry is not None:
                entries.append(entry)
        for entry in entries:
            task = asyncio.create_task(self._disconnect_entry(entry))
            self._invalidation_tasks.add(task)
            task.add_done_callback(self._invalidation_tasks.discard)

    async def _evict_idle(self) -> None:
        """Background loop that evicts idle tenant connections."""
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            await self._evict_idle_once(time.monotonic())

    async def _evict_idle_once(self, now: float) -> None:
        """Evict the tenant connections idle at *now*.  Their approved
        fingerprints stay (``_approvals``): the next connection is checked
        against them."""
        evicted: list[tuple[UUID, UUID, str]] = []

        for key, entry in list(self._entries.items()):
            if now - entry.last_used > self._idle_timeout:
                await self._disconnect_entry(entry)
                evicted.append(key)

        for key in evicted:
            self._entries.pop(key, None)
            self._locks.pop(key, None)
            logger.info(
                "Evicted idle MCP connections for org=%s user=%s agent=%s",
                key[0], key[1], key[2],
            )
