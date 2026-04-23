"""Tool routing through governance into the tool registry or sandbox.

The :class:`ToolRouter` sits between the harness loop and the actual tool
implementations.  Every call goes through:

1. Governance check (:meth:`GovernanceGate.check`).
2. Location resolution (:meth:`resolve_location`).
3. Dispatch — harness-local tools via :meth:`ToolRegistry.dispatch`,
   sandbox tools via :class:`SandboxPool`.

Harness-local tools (memory, skills, web_search, etc.) run in the worker
process.  Sandbox tools (terminal, file I/O, code execution) run in a
provisioned sandbox that is lazily created per session and reused across
tool calls.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any
from uuid import UUID

from surogates.governance.policy import GovernanceGate, PolicyDecision
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.base import SandboxSpec
from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolLocation(str, Enum):
    """Where a tool should be executed."""

    HARNESS = "harness"   # runs in the worker process
    SANDBOX = "sandbox"   # runs in a provisioned sandbox


TOOL_LOCATIONS: dict[str, ToolLocation] = {
    # Harness-local (no isolation needed)
    "memory": ToolLocation.HARNESS,
    "skills_list": ToolLocation.HARNESS,
    "skill_view": ToolLocation.HARNESS,
    "skill_manage": ToolLocation.HARNESS,
    "session_search": ToolLocation.HARNESS,
    "web_search": ToolLocation.HARNESS,
    "web_extract": ToolLocation.HARNESS,
    "web_crawl": ToolLocation.HARNESS,
    "clarify": ToolLocation.HARNESS,
    "delegate_task": ToolLocation.HARNESS,
    "todo": ToolLocation.HARNESS,
    "process": ToolLocation.HARNESS,
    "consult_expert": ToolLocation.HARNESS,
    "create_artifact": ToolLocation.HARNESS,
    # Coordinator tools (session management, no isolation needed)
    "spawn_worker": ToolLocation.HARNESS,
    "send_worker_message": ToolLocation.HARNESS,
    "stop_worker": ToolLocation.HARNESS,
    # Sandbox (code execution, file mutation, need isolation)
    "terminal": ToolLocation.SANDBOX,
    "read_file": ToolLocation.SANDBOX,
    "write_file": ToolLocation.SANDBOX,
    "patch": ToolLocation.SANDBOX,
    "search_files": ToolLocation.SANDBOX,
    "list_files": ToolLocation.SANDBOX,
}


class ToolRouter:
    """Routes tool calls through governance and into the registry or sandbox.

    Parameters
    ----------
    registry:
        The worker-local :class:`ToolRegistry` holding all registered tools.
    sandbox_pool:
        The :class:`SandboxPool` that manages session-to-sandbox mappings
        and dispatches sandbox tool calls.
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
    # Location resolution
    # ------------------------------------------------------------------

    def resolve_location(self, tool_name: str) -> ToolLocation:
        """Return where *tool_name* should execute.

        Tools listed in :data:`TOOL_LOCATIONS` use their explicit mapping.
        Unknown tools default to :attr:`ToolLocation.SANDBOX` so that any
        newly-registered tool that is not explicitly classified gets
        isolation by default.
        """
        return TOOL_LOCATIONS.get(tool_name, ToolLocation.SANDBOX)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        *,
        name: str,
        arguments: str | dict[str, Any],
        tenant: Any,
        session_id: UUID,
        workspace_path: str | None = None,
    ) -> str:
        """Route a tool call through governance and dispatch.

        Steps:

        1. Run :meth:`GovernanceGate.check` (includes workspace sandbox
           enforcement via AGT ``ExecutionSandbox``).  If denied, return
           a JSON error payload immediately.
        2. Resolve the tool location (harness vs. sandbox).
        3. Dispatch via the appropriate backend.

        Parameters
        ----------
        workspace_path:
            Absolute path to the session's workspace directory.  When set,
            all filesystem path arguments are validated to be within this
            directory before the tool is allowed to execute.
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

        decision: PolicyDecision = self.governance.check(
            name, parsed_args,
            workspace_path=workspace_path,
            session_id=str(session_id),
        )
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

        # -- 3. Dispatch ------------------------------------------------
        match location:
            case ToolLocation.HARNESS:
                return await self.registry.dispatch(
                    name,
                    arguments,
                    tenant=tenant,
                    session_id=session_id,
                )
            case ToolLocation.SANDBOX:
                # Lazily provision or reuse the session's sandbox.
                sandbox_spec = (
                    getattr(tenant, "sandbox_spec", None)
                    or SandboxSpec()
                )
                await self.sandbox_pool.ensure(
                    str(session_id), sandbox_spec,
                )
                # Serialise arguments to a JSON string for the sandbox.
                if isinstance(arguments, dict):
                    args_str = json.dumps(arguments)
                else:
                    args_str = arguments if arguments else "{}"
                return await self.sandbox_pool.execute(
                    str(session_id), name, args_str,
                )
