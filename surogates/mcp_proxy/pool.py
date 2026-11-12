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
import contextlib
import copy
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal
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
    TRANSIENT_CONNECT_FAILURES,
    _MCP_AVAILABLE,
    _UTILITY_OFFER,
    _connect_failures,
    _lock,
    _parse_boolish,
    _register_server_tools,
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
#: Seconds after a server's first failed connect before it is tried again,
#: in the background (a tool list never waits for it).  Each further failure
#: doubles it, up to _MAX_UNAVAILABLE_RETRY_COOLDOWN.  A server that failed
#: otherwise than on a timeout or a refusal (a config, auth or policy error,
#: or a server restarting) is tried again only every
#: _MAX_UNAVAILABLE_RETRY_COOLDOWN.
_UNAVAILABLE_RETRY_COOLDOWN = 120.0
_MAX_UNAVAILABLE_RETRY_COOLDOWN = 1800.0
#: The reason of a server left without a connection and with no failure on
#: record (connected, then shut down meanwhile): tried again as soon as a
#: timeout is, and told to the agent as ``error``.
_LOST = "lost"
_RETRIED_SOON = TRANSIENT_CONNECT_FAILURES | {_LOST}


class AgentChangedDuringConnect(Exception):
    """The agent was invalidated while its MCP servers were connecting: what
    that connect found is dropped, and its configs must be read anew."""


def _note_failed_attempt(entry: "PoolEntry", failed: dict[str, str]) -> None:
    """Record that a connect attempt of each of *failed* (name -> why) failed now."""
    now = time.monotonic()
    for name, why in failed.items():
        entry.unavailable[name] = why
        entry.failed_at[name] = now
        entry.failures[name] = entry.failures.get(name, 1) + 1


#: Seconds at least between two re-lists of one server's tools when it keeps
#: saying they changed (``ConnectionPool._tools_changed``).
_RELIST_MIN_INTERVAL = 5.0


def _definition_digest(tool: Any) -> str:
    """A registered MCP tool's definition as the proxy scans it (description,
    parameters, annotations), as a digest: equal while it is unchanged."""
    schema = tool.schema
    canonical = json.dumps(
        [schema.description, schema.parameters, schema.annotations], sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _retry_cooldown(failures: int) -> float:
    """Seconds to wait before trying a server again after *failures* failed attempts in a row."""
    return min(_UNAVAILABLE_RETRY_COOLDOWN * 2 ** max(failures - 1, 0), _MAX_UNAVAILABLE_RETRY_COOLDOWN)


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


def _discovery_config(config: dict[str, Any]) -> dict[str, Any]:
    """*config* as discovery gets it: with the resource / prompt utility
    tools registered whatever the server offers (a list leaves out those it
    does not offer, read from the live connection: ``visible_schemas``)."""
    return {**config, "tools": {**(config.get("tools") or {}), "utilities": "all"}}


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
    # Configured servers that could not be connected: original name -> why
    # (``timeout``, ``refused``, ``error`` or ``lost``).  Their tools are
    # missing from ``tool_schemas``; a tool list names them, and they are
    # tried again in the background once their cooldown is over
    # (``due_for_retry``).
    unavailable: dict[str, str] = field(default_factory=dict)
    # When each unavailable server's last connect attempt failed
    # (``time.monotonic``), and how many attempts in a row have failed.
    failed_at: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    # ``user_id`` is a service account's id (audit rows of a retry's scans).
    is_service_account: bool = False
    # Each listed tool's definition as it was scanned (``_definition_digest``),
    # by its listed name: a server that says its tools changed loses at once
    # the ones whose definition is no longer that (``_block_changed_tools``).
    digests: dict[str, str] = field(default_factory=dict)
    # Per server, the definitions its last scan was given, listed or not:
    # original tool name -> ``_definition_digest``.
    scanned: dict[str, dict[str, str]] = field(default_factory=dict)
    # When each server's tools were last listed anew (``time.monotonic``).
    relisted_at: dict[str, float] = field(default_factory=dict)

    def due_for_retry(self, now: float) -> list[str]:
        """The unavailable servers whose cooldown is over at *now*: after a
        timeout, a refusal or a lost connection 120 s doubling with each
        failure, after any other error the longest cooldown (it most likely
        fails the same way again)."""
        return sorted(
            name for name, why in self.unavailable.items()
            if now - self.failed_at.get(name, float("-inf")) >= (
                _retry_cooldown(self.failures.get(name, 1)) if why in _RETRIED_SOON
                else _MAX_UNAVAILABLE_RETRY_COOLDOWN
            )
        )


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
    scan_wording:
        ``"advisory"`` (default): a tool whose description trips only one
        of AGT's wording heuristics is kept, with the hit audited and
        logged (``governance.mcp_scanner``).  ``"drop"``: every hit drops
        the tool (proxy setting ``mcp_scan_wording``).
    """

    def __init__(
        self,
        idle_timeout: int = 300,
        max_per_org: int = 50,
        *,
        governance_enabled: bool = False,
        audit_store: AuditStore | None = None,
        scan_wording: Literal["advisory", "drop"] = "advisory",
    ) -> None:
        self._scan_wording = scan_wording
        self._idle_timeout = idle_timeout
        self._max_per_org = max_per_org
        self._entries: dict[tuple[UUID, UUID, str], PoolEntry] = {}
        self._locks: dict[tuple[UUID, UUID, str], asyncio.Lock] = {}
        # Background agent-eviction tasks. Held so they are not garbage-
        # collected mid-flight (CPython only keeps weak refs to tasks);
        # each removes itself via a done-callback.
        self._invalidation_tasks: set[asyncio.Task] = set()
        # The background retry of each tenant's unavailable servers, one at
        # a time per tenant (``retry_unavailable_servers``).
        self._retry_tasks: dict[tuple[UUID, UUID, str], asyncio.Task] = {}
        # A server's tools listed anew after it said they changed
        # (``notifications/tools/list_changed``): one re-list at a time per
        # tenant and server, and one more queued when it says so again
        # meanwhile (``_tools_changed``).
        self._relist_tasks: dict[tuple[tuple[UUID, UUID, str], str], asyncio.Task] = {}
        self._relist_again: set[tuple[tuple[UUID, UUID, str], str]] = set()
        # Set by ``shutdown``: a late notification starts nothing.
        self._closed = False
        # Per agent, how many times it was invalidated: a connect that sees
        # the count change while it runs keeps nothing (``ensure_connected``).
        self._agent_epochs: dict[str, int] = {}
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
        self._closed = True
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
        for task in list(self._retry_tasks.values()):
            task.cancel()
        self._retry_tasks.clear()
        self._relist_again.clear()
        for task in list(self._relist_tasks.values()):
            task.cancel()
        self._relist_tasks.clear()

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

    def unavailable_servers(
        self, org_id: UUID, user_id: UUID, agent_id: str,
    ) -> dict[str, str]:
        """The agent's configured servers that could not be connected at its
        last attempt: original name -> ``timeout`` / ``refused`` / ``error``."""
        entry = self._entries.get((org_id, user_id, agent_id))
        if entry is None:
            return {}
        return {name: "error" if why == _LOST else why for name, why in entry.unavailable.items()}

    def agent_epoch(self, agent_id: str) -> int:
        """How many times *agent_id* was invalidated: read before its configs
        are loaded, and passed to :meth:`ensure_connected`."""
        return self._agent_epochs.get(agent_id, 0)

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
        epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        """Ensure MCP servers for this tenant are connected and return
        their tool schemas.

        *epoch* is :meth:`agent_epoch` as it was when *configs* were loaded
        (default: now).  If the agent is invalidated before or while this
        connects, it keeps nothing and raises
        :class:`AgentChangedDuringConnect`: the caller loads the configs anew.

        Idempotent — if already connected, returns cached schemas.  A
        configured server that could not be connected is left out (named in
        ``PoolEntry.unavailable``) and tried again in the background
        (:meth:`retry_unavailable_servers`), never while a list waits.

        ``is_service_account`` marks ``user_id`` as a service-account id
        rather than a real ``users.id``; downstream audit writes use
        ``user_id=None`` in that case to avoid FK violations.
        """
        key = (org_id, user_id, agent_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if epoch is None:
            epoch = self.agent_epoch(agent_id)

        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
                self._touch_approvals(key)
                return entry.tool_schemas
            if self.agent_epoch(agent_id) != epoch:
                raise AgentChangedDuringConnect(agent_id)

            # Prefix each server name for tenant isolation.
            prefixed: dict[str, dict[str, Any]] = {}
            original_names: list[str] = []
            for name, cfg in configs.items():
                pname = _prefixed_name(org_id, user_id, agent_id, name)
                prefixed[pname] = _discovery_config(cfg)
                original_names.append(name)

            from surogates.tools.registry import ToolRegistry

            temp_registry = ToolRegistry()
            # A server of this tenant still connected (discovery skips it) is
            # listed from its live connection.
            with _lock:
                adopted = {pname: _servers[pname] for pname in prefixed if pname in _servers}
            for pname, server in adopted.items():
                _register_server_tools(pname, server, prefixed[pname], temp_registry)
            await asyncio.to_thread(
                discover_mcp_tools,
                servers=prefixed,
                registry=temp_registry,
            )
            # The servers connected now (read again: one adopted above may
            # have been shut down meanwhile), and the enabled ones left
            # without a connection, with why.
            with _lock:
                owners = {pname: _servers[pname] for pname in prefixed if pname in _servers}
                unavailable = {
                    name: _connect_failures.pop(pname, _LOST)
                    for pname, name in zip(prefixed, original_names)
                    if pname not in _servers
                    and _parse_boolish(configs[name].get("enabled", True), default=True)
                }

            # Per-tenant MCPGovernance over the tenant's approved
            # fingerprints, keyed by (server, tool), never shared with
            # other tenants even when server names collide.
            tenant_governance: MCPGovernance | None = None
            approval_room: _ApprovalRoom | None = None
            if self._governance_enabled:
                approvals = self._tenant_approvals(key)
                tenant_governance = MCPGovernance(fingerprints=approvals, wording=self._scan_wording)
                approval_room = _ApprovalRoom(approvals)

            clean_schemas, tool_index, utility_index = [], {}, {}
            digests: dict[str, str] = {}
            if self.agent_epoch(agent_id) == epoch:
                clean_schemas, tool_index, utility_index = await self._clean_schemas(
                    key, temp_registry, owners, configs,
                    governance=tenant_governance, approval_room=approval_room,
                    is_service_account=is_service_account, taken=set(), digests=digests,
                )
            if self.agent_epoch(agent_id) != epoch:
                # The agent was invalidated while this connected: its entry
                # must not come back, and what this call connected or adopted
                # belongs to no entry.  Shut down before the lock is let go,
                # so the next connect starts clean.
                await self._shutdown_servers(owners)
                logger.info(
                    "MCP connect for org=%s user=%s agent=%s dropped: the agent changed meanwhile",
                    org_id, user_id, agent_id,
                )
                raise AgentChangedDuringConnect(agent_id)

            failed_at = time.monotonic()
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
                sanitized_prefix=sanitize_mcp_name_component(
                    _tenant_prefix(org_id, user_id, agent_id),
                ),
                governance=tenant_governance,
                utility_index=utility_index,
                unavailable=unavailable,
                failed_at={name: failed_at for name in unavailable},
                failures={name: 1 for name in unavailable},
                is_service_account=is_service_account,
                digests=digests,
            )
            self._entries[key] = entry
            self._follow_tool_changes(key, {
                name: owners[pname] for pname, name in zip(prefixed, original_names) if pname in owners
            })

            logger.info(
                "Connected %d MCP server(s) for org=%s user=%s agent=%s "
                "(%d tools)",
                len(original_names), org_id, user_id, agent_id,
                len(clean_schemas),
            )
            if unavailable:
                logger.warning(
                    "MCP server(s) unavailable for org=%s user=%s agent=%s: %s "
                    "-- tried again in the background (after %.0fs on a timeout or refusal)",
                    org_id, user_id, agent_id,
                    ", ".join(f"{name} ({why})" for name, why in sorted(unavailable.items())),
                    _UNAVAILABLE_RETRY_COOLDOWN,
                )
            return clean_schemas

    async def _clean_schemas(
        self,
        key: tuple[UUID, UUID, str],
        temp_registry: Any,
        owners: dict[str, Any],
        configs: dict[str, dict[str, Any]],
        *,
        governance: MCPGovernance | None,
        approval_room: _ApprovalRoom | None,
        is_service_account: bool,
        taken: set[str],
        digests: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]], dict[str, str]]:
        """The advertised schemas of the tools in *temp_registry* that the
        connected servers *owners* (tenant-scoped key -> server) registered,
        with their reverse index and utility index.

        A tool no server in *owners* owns is never listed: its name is the
        tenant-scoped key, with the org / user / agent ids.  Names in *taken*
        are already advertised.
        """
        org_id, user_id, agent_id = key
        tenant_key = f"{_tenant_prefix(org_id, user_id, agent_id)}__"

        # ``ToolRegistry.get_schemas`` emits OpenAI's nested
        # ``{"type":"function","function":{"name",...}}`` shape; the
        # rest of this method (and the ``/mcp/v1/tools/list`` route
        # contract) work on a flat ``{name,description,parameters}``
        # shape, so unwrap once here. Skipping this step is what
        # makes the proxy advertise empty-named tools and the worker
        # silently register zero MCP tools.
        schemas = [
            _flatten_openai_schema(s)
            for s in temp_registry.get_schemas(names=set(temp_registry.tool_names))
        ]
        for s in schemas:
            tool = temp_registry.get(s["name"])
            if tool is not None and tool.schema.annotations:
                s["annotations"] = tool.schema.annotations

        clean_schemas: list[dict[str, Any]] = []
        seen_names: set[str] = set(taken)
        tool_index: dict[str, tuple[str, str]] = {}
        utility_index: dict[str, str] = {}

        for schema in schemas:
            clean = dict(schema)
            # The proxy's own name for the tool, under the tenant-scoped
            # server key (``mcp__{tenant_prefix}__{server}__{tool}``,
            # shortened: it is never parsed back).
            raw_name = clean.get("name", "")

            # Build reverse index for O(1) tool call routing.
            # Find which prefixed server owns this tool, and the MCP
            # name the tool is called by there.
            owning_server_key: str | None = None
            original_tool: str = ""
            utility: str | None = None
            with _lock:
                for server_key, server in owners.items():
                    if raw_name in (getattr(server, "_registered_tool_names", None) or ()):
                        owning_server_key = server_key
                        original_tool = (getattr(server, "_tool_names", None) or {}).get(raw_name, raw_name)
                        utility = (getattr(server, "_utility_names", None) or {}).get(raw_name)
                        break
            if owning_server_key is None:
                logger.warning("MCP tool %s of no connected server left out of the tool list", raw_name)
                continue
            plain = owning_server_key[len(tenant_key):] if owning_server_key.startswith(tenant_key) else owning_server_key
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
            if governance is not None:
                original_server = plain

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
                    governance=governance,
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
            tool_index[clean_name] = (owning_server_key, original_tool)
            if utility is not None:
                utility_index[clean_name] = utility
            if digests is not None:
                digests[clean_name] = _definition_digest(temp_registry.get(raw_name))
        return clean_schemas, tool_index, utility_index

    # ------------------------------------------------------------------
    # Background retry of unavailable servers
    # ------------------------------------------------------------------

    def retry_unavailable_servers(self, org_id: UUID, user_id: UUID, agent_id: str) -> None:
        """Start, in the background, a retry of the agent's unavailable
        servers whose cooldown is over (:meth:`PoolEntry.due_for_retry`).

        One retry at a time per tenant, one connect attempt per server; it
        adds a server's tools to the entry when it connects.  Returns at
        once: a tool list never waits for it.
        """
        key = (org_id, user_id, agent_id)
        entry = self._entries.get(key)
        if entry is None or key in self._retry_tasks:
            return
        due = entry.due_for_retry(time.monotonic())
        if not due:
            return
        task = asyncio.create_task(self._retry_servers(key, entry, due), name="mcp-pool-retry")
        self._retry_tasks[key] = task

        def _done(done: asyncio.Task) -> None:
            if self._retry_tasks.get(key) is done:
                del self._retry_tasks[key]
            # Nothing awaits the task: what it raised is logged here, not
            # left to "Task exception was never retrieved".
            if not done.cancelled() and (error := done.exception()) is not None:
                logger.error(
                    "MCP background retry task for org=%s user=%s agent=%s raised",
                    key[0], key[1], key[2], exc_info=error,
                )

        task.add_done_callback(_done)

    async def _retry_servers(self, key: tuple[UUID, UUID, str], entry: PoolEntry, due: list[str]) -> None:
        """Try *due* (unavailable servers of *entry*) once more each, and
        fold the result into *entry* unless it was invalidated, evicted or
        replaced meanwhile."""
        from surogates.tools.registry import ToolRegistry

        names_by_key = {
            _prefixed_name(*key, name): name for name in due if name in entry.server_configs
        }
        prefixed = {
            pname: _discovery_config(entry.server_configs[name]) for pname, name in names_by_key.items()
        }
        temp_registry = ToolRegistry()
        # A due server already connected (discovery skips it) is listed
        # from its live connection, as at the first connect.
        with _lock:
            adopted = {pname: _servers[pname] for pname in prefixed if pname in _servers}
        for pname, server in adopted.items():
            _register_server_tools(pname, server, prefixed[pname], temp_registry)
        try:
            await asyncio.to_thread(
                discover_mcp_tools, servers=prefixed, registry=temp_registry, retry=False,
            )
        except Exception:
            logger.warning("MCP background retry for agent %s failed", key[2], exc_info=True)
        with _lock:
            owners = {pname: _servers[pname] for pname in prefixed if pname in _servers}
            reasons = {
                name: _connect_failures.pop(pname, _LOST)
                for pname, name in names_by_key.items() if pname not in _servers
            }

        clean_schemas, tool_index, utility_index = [], {}, {}
        digests: dict[str, str] = {}
        if owners and self._entries.get(key) is entry:
            # Only the due servers' tools are scanned: the live ones keep the
            # schemas they were approved with.
            try:
                clean_schemas, tool_index, utility_index = await self._clean_schemas(
                    key, temp_registry, owners, entry.server_configs,
                    governance=entry.governance,
                    approval_room=_ApprovalRoom(self._tenant_approvals(key)) if entry.governance is not None else None,
                    is_service_account=entry.is_service_account,
                    taken={s.get("name", "") for s in entry.tool_schemas},
                    digests=digests,
                )
            except Exception:
                # The scan raised (a scanner or audit-emitter error, which a
                # tool definition can trigger every time): the entry still
                # names them unavailable, so the attempt counts as failed and
                # the next one waits for its cooldown, not for the next tool
                # list; and what this retry connected must not stay open
                # outside the entry.
                logger.error(
                    "MCP background retry for org=%s user=%s agent=%s: scanning the reconnected servers' tools raised",
                    key[0], key[1], key[2], exc_info=True,
                )
                if self._entries.get(key) is entry:
                    _note_failed_attempt(entry, {
                        **{names_by_key[p]: entry.unavailable.get(names_by_key[p], _LOST) for p in owners},
                        **reasons,
                    })
                await self._shutdown_servers(owners)
                return
            except BaseException:
                await self._shutdown_servers(owners)
                raise
        if self._entries.get(key) is not entry:
            await self._shutdown_servers(owners)
            return

        # A name another server's tool took meanwhile (a re-list) stays with it.
        taken = {s.get("name", "") for s in entry.tool_schemas}
        clean_schemas = [s for s in clean_schemas if s["name"] not in taken]
        entry.tool_schemas = [*entry.tool_schemas, *clean_schemas]
        entry.tool_index = {**entry.tool_index, **{n: r for n, r in tool_index.items() if n not in taken}}
        entry.utility_index = {**entry.utility_index, **{n: u for n, u in utility_index.items() if n not in taken}}
        entry.digests = {**entry.digests, **{n: d for n, d in digests.items() if n not in taken}}
        self._follow_tool_changes(key, {names_by_key[pname]: server for pname, server in owners.items()})
        for pname in owners:
            name = names_by_key[pname]
            entry.unavailable.pop(name, None)
            entry.failed_at.pop(name, None)
            entry.failures.pop(name, None)
        _note_failed_attempt(entry, reasons)
        logger.info(
            "MCP background retry for org=%s user=%s agent=%s: connected %s; still unavailable %s",
            key[0], key[1], key[2],
            ", ".join(sorted(names_by_key[p] for p in owners)) or "none",
            ", ".join(
                f"{name} ({why}, next try in "
                f"{_retry_cooldown(entry.failures[name]) if why in _RETRIED_SOON else _MAX_UNAVAILABLE_RETRY_COOLDOWN:.0f}s)"
                for name, why in sorted(reasons.items())
            ) or "none",
        )

    # ------------------------------------------------------------------
    # A server whose tools changed (notifications/tools/list_changed)
    # ------------------------------------------------------------------

    def _follow_tool_changes(self, key: tuple[UUID, UUID, str], servers: dict[str, Any]) -> None:
        """Have each of *servers* (configured name -> connected server) of
        tenant *key* tell this pool when its tool list changes: the MCP client
        lists its tools anew on ``notifications/tools/list_changed`` and calls
        ``_on_tools_changed`` on its own loop; the pool then follows on its
        loop (:meth:`_tools_changed`).  A change that came before this wiring
        (while the tools were scanned) is followed at once."""
        loop = asyncio.get_running_loop()
        entry = self._entries.get(key)
        for name, server in servers.items():
            def changed(_server_name: str, _tool_names: list[str], name: str = name, server: Any = server) -> None:
                # A pool whose loop has closed lists nothing any more.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._tools_changed, key, name, server)
            server._on_tools_changed = changed
            if entry is None:
                continue
            # What its scan was given.  A listed tool changed meanwhile is
            # taken off and listed anew; a tool added meanwhile waits for the
            # server's next notification.
            current = self._block_changed_tools(key, entry, name, server)
            if current is None:
                self._tools_changed(key, name, server)
            else:
                entry.scanned[name] = current

    def _tools_changed(self, key: tuple[UUID, UUID, str], name: str, server: Any) -> None:
        """*server* (configured *name*) of tenant *key* may list other tools now.

        At once: its listed tools whose definition is no longer the one
        scanned, or that it no longer has, are taken off the entry (neither
        listed nor callable).  Then, when anything differs, its tools are
        listed anew (:meth:`_relist_server`): one re-list at a time per tenant
        and server, at most one every ``_RELIST_MIN_INTERVAL`` seconds, and one
        more queued when it says so again meanwhile."""
        if self._closed:
            return
        entry = self._entries.get(key)
        if entry is None:
            return
        current = self._block_changed_tools(key, entry, name, server)
        if current is not None and current == entry.scanned.get(name):
            return  # nothing it lists differs from what was scanned
        job = (key, name)
        task = self._relist_tasks.get(job)
        if task is not None and not task.done():
            self._relist_again.add(job)
            return
        delay = max(0.0, entry.relisted_at.get(name, float("-inf")) + _RELIST_MIN_INTERVAL - time.monotonic())
        task = asyncio.create_task(self._relist_server(key, name, server, delay=delay), name="mcp-pool-relist")
        self._relist_tasks[job] = task

        def _done(done: asyncio.Task) -> None:
            if self._relist_tasks.get(job) is done:
                del self._relist_tasks[job]
            if done.cancelled():
                self._relist_again.discard(job)
                return
            if (error := done.exception()) is not None:
                logger.error(
                    "MCP tool re-list for org=%s user=%s agent=%s server %s raised",
                    key[0], key[1], key[2], name, exc_info=error,
                )
            if job in self._relist_again:
                self._relist_again.discard(job)
                self._tools_changed(key, name, server)

        task.add_done_callback(_done)

    def _current_tools(self, key: tuple[UUID, UUID, str], name: str, server: Any, config: dict[str, Any]) -> tuple[Any, Any]:
        """*server*'s tools as listed now: a registry of them and the copy of
        the server they were registered from.  Read from a copy: the
        connection's own name maps are the MCP client's, on its own loop."""
        from surogates.tools.registry import ToolRegistry

        listed = copy.copy(server)
        registry = ToolRegistry()
        listed._registered_tool_names = _register_server_tools(
            _prefixed_name(*key, name), listed, _discovery_config(config), registry,
        )
        return registry, listed

    def _block_changed_tools(
        self, key: tuple[UUID, UUID, str], entry: PoolEntry, name: str, server: Any,
    ) -> dict[str, str] | None:
        """Take off *entry* the listed tools of *server* whose definition
        changed since their scan, or that it no longer has.  Returns its
        tools' definitions now (original name -> digest), or None when one
        was taken off or the server is not this entry's live connection."""
        pname = _prefixed_name(*key, name)
        config = entry.server_configs.get(name)
        with _lock:
            live = _servers.get(pname) is server
        if config is None or not live:
            return None
        registry, listed = self._current_tools(key, name, server, config)
        now = {
            listed._tool_names.get(raw, raw): _definition_digest(registry.get(raw))
            for raw in listed._registered_tool_names
        }
        theirs = {n: original for n, (owner, original) in entry.tool_index.items() if owner == pname}
        changed = {n for n, original in theirs.items() if now.get(original) != entry.digests.get(n)}
        if changed:
            entry.tool_schemas = [s for s in entry.tool_schemas if s.get("name", "") not in changed]
            entry.tool_index = {n: r for n, r in entry.tool_index.items() if n not in changed}
            entry.utility_index = {n: u for n, u in entry.utility_index.items() if n not in changed}
            entry.digests = {n: d for n, d in entry.digests.items() if n not in changed}
            logger.warning(
                "MCP server %s of org=%s user=%s agent=%s changed or removed %s: off the tool list until re-listed",
                name, key[0], key[1], key[2], ", ".join(sorted(changed)),
            )
        return None if changed else now

    async def _relist_server(self, key: tuple[UUID, UUID, str], name: str, server: Any, *, delay: float = 0.0) -> None:
        """List *server*'s tools anew in tenant *key*'s entry: scanned as at
        a connect (a tool whose definition changed after its approval is a
        rug-pull: audited and left out), then put in place of the server's
        tools there.  Nothing is done for an entry or a connection that is
        gone or replaced."""
        if delay:
            await asyncio.sleep(delay)
        entry = self._entries.get(key)
        config = entry.server_configs.get(name) if entry is not None else None
        pname = _prefixed_name(*key, name)
        with _lock:
            live = _servers.get(pname) is server
        if entry is None or config is None or not live:
            return
        entry.relisted_at[name] = time.monotonic()
        registry, listed = self._current_tools(key, name, server, config)
        given = {
            listed._tool_names.get(raw, raw): _definition_digest(registry.get(raw))
            for raw in listed._registered_tool_names
        }

        def theirs() -> set[str]:
            return {n for n, (owner, _tool) in entry.tool_index.items() if owner == pname}

        digests: dict[str, str] = {}
        clean_schemas, tool_index, utility_index = await self._clean_schemas(
            key, registry, {pname: listed}, entry.server_configs,
            governance=entry.governance,
            approval_room=_ApprovalRoom(self._tenant_approvals(key)) if entry.governance is not None else None,
            is_service_account=entry.is_service_account,
            taken={s.get("name", "") for s in entry.tool_schemas} - theirs(),
            digests=digests,
        )
        with _lock:
            live = _servers.get(pname) is server
        if self._entries.get(key) is not entry or not live:
            return
        old = theirs()
        # Another server's tool may have taken a name meanwhile: it keeps it.
        taken = {s.get("name", "") for s in entry.tool_schemas} - old
        new = {s["name"]: s for s in clean_schemas if s["name"] not in taken}
        # In place: a tool listed before keeps its place in the list.
        kept = [new.pop(s["name"]) if s.get("name") in new else s for s in entry.tool_schemas
                if s.get("name", "") not in old or s.get("name") in new]
        listed_now = {s["name"] for s in clean_schemas if s["name"] not in taken}
        entry.tool_schemas = [*kept, *new.values()]
        entry.tool_index = {
            **{n: r for n, r in entry.tool_index.items() if n not in old},
            **{n: r for n, r in tool_index.items() if n in listed_now},
        }
        entry.utility_index = {
            **{n: u for n, u in entry.utility_index.items() if n not in old},
            **{n: u for n, u in utility_index.items() if n in listed_now},
        }
        entry.digests = {
            **{n: d for n, d in entry.digests.items() if n not in old},
            **{n: d for n, d in digests.items() if n in listed_now},
        }
        entry.scanned[name] = given
        added, removed = listed_now - old, old - listed_now
        logger.log(
            logging.INFO if added or removed else logging.DEBUG,
            "MCP server %s of org=%s user=%s agent=%s listed anew: %d tool(s); added %s; removed %s",
            name, key[0], key[1], key[2], len(listed_now),
            ", ".join(sorted(added)) or "none", ", ".join(sorted(removed)) or "none",
        )

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
                "Unsafe MCP tool %s from server %s [%s]: %s%s",
                tool_name, server_name, result.severity,
                "; ".join(result.threats),
                f" (advisories: {'; '.join(result.advisories)})" if result.advisories else "",
            )
            return False

        if result.advisories:
            # Once per approval, not at every reconnect of an approved tool.
            logger.log(
                logging.DEBUG if governance.has_fingerprint(qualified_name) else logging.WARNING,
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
        await self._shutdown(servers_to_shutdown)

    async def _shutdown_servers(self, servers: dict[str, Any]) -> None:
        """Remove *servers* (tenant-scoped key -> server) from the
        module-level dict, where they are still those, and shut them down."""
        with _lock:
            gone = {key: server for key, server in servers.items() if _servers.get(key) is server}
            for key in gone:
                del _servers[key]
        await self._shutdown(list(gone.values()))

    @staticmethod
    async def _shutdown(servers_to_shutdown: list[Any]) -> None:
        """Shut *servers_to_shutdown* down on the loop their sessions run on."""
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
        # A connect in progress sees the count change and keeps nothing
        # (``ensure_connected``); its lock stays, so the next connect waits
        # for it to clean up.
        self._agent_epochs[agent_id] = self.agent_epoch(agent_id) + 1
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
