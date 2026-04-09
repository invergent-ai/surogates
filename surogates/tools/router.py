"""Tool routing through governance and into the correct execution backend.

The :class:`ToolRouter` sits between the harness loop and the actual tool
implementations.  Every call goes through:

1. Governance check (:meth:`GovernanceGate.check`).
2. Location resolution (harness-local, sandbox, or MCP).
3. Dispatch to the appropriate backend.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any
from uuid import UUID

from surogates.governance.policy import GovernanceGate, PolicyDecision
from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.pool import SandboxPool
from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolLocation(str, Enum):
    """Where a tool's handler is executed."""

    HARNESS = "harness"
    SANDBOX = "sandbox"
    MCP = "mcp"


# Default classification for builtin tools.
TOOL_LOCATIONS: dict[str, ToolLocation] = {
    # Harness-local tools -- run in the harness process.
    "memory": ToolLocation.HARNESS,
    "memory_read": ToolLocation.HARNESS,
    "memory_write": ToolLocation.HARNESS,
    "skills_list": ToolLocation.HARNESS,
    "skill_manage": ToolLocation.HARNESS,
    "session_search": ToolLocation.HARNESS,
    "web_search": ToolLocation.HARNESS,
    "delegate_task": ToolLocation.HARNESS,
    # Sandbox tools -- forwarded to the sandbox runtime.
    "terminal": ToolLocation.SANDBOX,
    "file_write": ToolLocation.SANDBOX,
    "file_read": ToolLocation.SANDBOX,
    "browser_navigate": ToolLocation.SANDBOX,
}


class ToolRouter:
    """Routes tool calls through governance and into the correct backend.

    Parameters
    ----------
    registry:
        The worker-local :class:`ToolRegistry` holding all registered tools.
    sandbox_pool:
        The :class:`SandboxPool` used for sandbox-located tools.
    governance:
        The :class:`GovernanceGate` that enforces allow/deny policies.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        sandbox_pool: SandboxPool,
        governance: GovernanceGate,
    ) -> None:
        self.registry = registry
        self.sandbox_pool = sandbox_pool
        self.governance = governance
        self._location_overrides: dict[str, ToolLocation] = {}
        self._mcp_prefixes: list[str] = []

    # ------------------------------------------------------------------
    # Schema export
    # ------------------------------------------------------------------

    def get_tool_schemas(
        self,
        allowed_tools: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get OpenAI-format schemas, filtered by *allowed_tools*.

        If *allowed_tools* is ``None``, every registered tool is returned.
        """
        return self.registry.get_schemas(names=allowed_tools)

    # ------------------------------------------------------------------
    # Location management
    # ------------------------------------------------------------------

    def set_location_override(self, tool_name: str, location: ToolLocation) -> None:
        """Override the default location for *tool_name*."""
        self._location_overrides[tool_name] = location

    def add_mcp_prefix(self, prefix: str) -> None:
        """Register a prefix (e.g. ``"github_"``) that marks MCP tools."""
        if prefix not in self._mcp_prefixes:
            self._mcp_prefixes.append(prefix)

    def resolve_location(self, tool_name: str) -> ToolLocation:
        """Resolve where a tool should execute.

        Resolution order:
        1. Per-instance overrides (set via :meth:`set_location_override`).
        2. The static :data:`TOOL_LOCATIONS` mapping.
        3. MCP prefix matching (any tool whose name starts with a
           registered MCP prefix).
        4. Default to :attr:`ToolLocation.HARNESS`.
        """
        # 1. Overrides.
        if tool_name in self._location_overrides:
            return self._location_overrides[tool_name]

        # 2. Static mapping.
        if tool_name in TOOL_LOCATIONS:
            return TOOL_LOCATIONS[tool_name]

        # 3. MCP wildcard prefix matching.
        for prefix in self._mcp_prefixes:
            if tool_name.startswith(prefix):
                return ToolLocation.MCP

        # 4. Default.
        return ToolLocation.HARNESS

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        tenant: Any,  # TenantContext -- typed as Any to avoid hard import
        session_id: UUID,
        sandbox_spec: SandboxSpec | None = None,
    ) -> str:
        """Route a tool call through governance and into the correct backend.

        Steps:

        1. Run :meth:`GovernanceGate.check`.  If denied, return a JSON
           error payload immediately.
        2. Resolve the tool's location.
        3. Dispatch to the matching backend:
           - **HARNESS** -- :meth:`ToolRegistry.dispatch`.
           - **SANDBOX** -- :meth:`SandboxPool.execute`.
           - **MCP** -- not yet implemented (returns an error message).
        """
        # -- 1. Governance check ----------------------------------------
        parsed_args: dict[str, Any] | None = None
        if isinstance(arguments, dict):
            parsed_args = arguments
        elif isinstance(arguments, str):
            try:
                parsed_args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                parsed_args = None

        decision: PolicyDecision = self.governance.check(name, parsed_args)
        if not decision.allowed:
            logger.warning(
                "Governance denied tool %s: %s", name, decision.reason,
            )
            return json.dumps(
                {
                    "error": "policy_denied",
                    "tool": name,
                    "reason": decision.reason,
                }
            )

        # -- 2. Resolve location ----------------------------------------
        location = self.resolve_location(name)

        # -- 3. Dispatch to backend -------------------------------------
        session_id_str = str(session_id)

        if location == ToolLocation.HARNESS:
            return await self.registry.dispatch(
                name,
                arguments,
                tenant=tenant,
                session_id=session_id,
            )

        if location == ToolLocation.SANDBOX:
            # Ensure a sandbox exists for this session.
            spec = sandbox_spec or SandboxSpec()
            await self.sandbox_pool.ensure(session_id_str, spec)

            # Serialise arguments for the sandbox runtime.
            input_str: str
            if isinstance(arguments, dict):
                input_str = json.dumps(arguments)
            else:
                input_str = arguments

            return await self.sandbox_pool.execute(
                session_id_str,
                name,
                input_str,
            )

        if location == ToolLocation.MCP:
            # Phase 3 -- MCP transport not yet wired.
            from surogates.tools.mcp import MCPClient

            return json.dumps(
                {
                    "error": "mcp_not_configured",
                    "tool": name,
                    "message": (
                        "MCP tool execution is not yet available.  "
                        "This tool requires an MCP server connection."
                    ),
                }
            )

        # Should never reach here, but be defensive.
        return json.dumps({"error": "unknown_location", "tool": name})
