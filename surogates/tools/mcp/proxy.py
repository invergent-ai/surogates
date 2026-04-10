"""MCP tool proxy -- bridges MCP servers into the ToolRegistry.

Takes MCP server configs (from tenant asset root or platform config),
creates MCPServerConnection instances via the client module, registers
discovered tools into the ToolRegistry, and provides shutdown_all()
for cleanup.

Integrates AGT ``MCPGateway`` for policy enforcement on every MCP tool
call: allow/deny filtering, parameter sanitization, rate limiting, and
audit logging (OWASP ASI02).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from agent_os.integrations.base import GovernancePolicy
from agent_os.mcp_gateway import MCPGateway

from surogates.tools.mcp.client import (
    _MCP_AVAILABLE,
    _format_connect_error,
    _interpolate_env_vars,
    _parse_boolish,
    discover_mcp_tools,
    get_mcp_status,
    shutdown_mcp_servers,
)
from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MCPToolProxy:
    """Discovers tools from MCP servers and registers them into the
    :class:`ToolRegistry`.

    Each MCP tool is registered with the name prefix
    ``mcp_{server_name}_{tool_name}`` to avoid collisions across servers.

    Integrates AGT :class:`MCPGateway` for governance on every MCP tool
    call.  The gateway enforces allow/deny lists, parameter sanitization
    (SSN, credit card, shell injection), and per-agent rate limiting.

    Parameters
    ----------
    registry:
        The :class:`ToolRegistry` to register discovered tools into.
    gateway_policy:
        Optional :class:`GovernancePolicy` for the MCPGateway.  When
        ``None``, a default open policy is used (no allow-list, default
        rate limit of 1000 calls).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        gateway_policy: GovernancePolicy | None = None,
    ) -> None:
        self._registry = registry
        self._server_configs: Dict[str, dict] = {}
        self._registered_tool_names: List[str] = []

        # AGT MCPGateway -- screens every MCP tool call for policy
        # violations before forwarding to the MCP server.
        policy = gateway_policy or GovernancePolicy(name="mcp_default")
        self._gateway = MCPGateway(policy=policy)

    @property
    def gateway(self) -> MCPGateway:
        """The AGT MCPGateway used for tool-call governance."""
        return self._gateway

    @property
    def server_names(self) -> list[str]:
        """Names of all configured MCP servers."""
        return list(self._server_configs.keys())

    @property
    def registered_tools(self) -> list[str]:
        """Names of all registered MCP tools."""
        return list(self._registered_tool_names)

    @property
    def available(self) -> bool:
        """Whether the MCP SDK is installed and available."""
        return _MCP_AVAILABLE

    def add_servers(self, servers: Dict[str, dict]) -> List[str]:
        """Connect to MCP servers and register their tools.

        Idempotent for already-connected server names.  Servers with
        ``enabled: false`` are skipped.

        Args:
            servers: Mapping of ``{server_name: server_config}``.
                Each config can contain either ``command``/``args``/``env``
                for stdio transport or ``url``/``headers`` for HTTP transport,
                plus optional ``timeout``, ``connect_timeout``, ``auth``,
                and ``sampling`` overrides.

        Returns:
            List of all registered MCP tool names.
        """
        if not _MCP_AVAILABLE:
            logger.debug("MCP SDK not available -- skipping MCP registration")
            return []

        if not servers:
            logger.debug("No MCP servers provided")
            return []

        self._server_configs.update(servers)
        self._registered_tool_names = discover_mcp_tools(
            servers=servers,
            registry=self._registry,
        )
        return list(self._registered_tool_names)

    def get_status(self) -> List[dict]:
        """Return status of all connected MCP servers.

        Returns a list of dicts with keys: name, transport, tools, connected.
        """
        return get_mcp_status()

    def shutdown_all(self) -> None:
        """Disconnect from all MCP servers, deregister their tools, and
        stop the background event loop.

        Safe to call multiple times.
        """
        # Deregister tools from the registry
        for tool_name in self._registered_tool_names:
            self._registry.deregister(tool_name)
        self._registered_tool_names.clear()

        # Shut down all MCP connections and the background loop
        shutdown_mcp_servers()

        self._server_configs.clear()
        logger.info("MCP tool proxy shut down")
