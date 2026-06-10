"""Built-in ``run_coding_agent`` tool — run Claude Code / Codex from the LLM.

Worker-local (mirrors ``consult_expert``).  The LLM hands a task to an external
coding agent running on the *user's own* connected plan inside the session
sandbox; the streamed run renders as a CodeRunBlock and the final message is
returned to the calling LLM so it can act on it.

Required kwargs (injected by the harness dispatch): ``tenant``, ``session_id``,
``session_store``, ``sandbox_pool``, ``credential_vault``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from surogates.coding_agents.command import AGENT_TO_PROVIDER
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.run_core import execute_coding_run
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_SCHEMA = ToolSchema(
    name="run_coding_agent",
    description=(
        "Run an external coding agent (Claude Code or Codex) on the current "
        "workspace using the user's connected plan. Use 'claude' to implement "
        "or edit code, 'codex' to review and run tests. The agent works on the "
        "shared /workspace and returns its final message. One run is one shot — "
        "it cannot pause to ask questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "codex"]},
            "prompt": {
                "type": "string",
                "description": "The task for the coding agent.",
            },
            "model": {"type": "string"},
            "effort": {
                "type": "string",
                "enum": ["low", "medium", "high", "xhigh"],
            },
            "read_only": {
                "type": "boolean",
                "description": "Run without writing changes.",
            },
        },
        "required": ["agent", "prompt"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``run_coding_agent`` tool."""
    registry.register(
        name="run_coding_agent",
        schema=_SCHEMA,
        handler=_run_coding_agent_handler,
        toolset="code",
    )


def _build_ensure(
    sandbox_pool: Any, session: Any, tenant: Any, owner: str,
) -> Callable[[], Awaitable[None]]:
    async def _ensure() -> None:
        from surogates.harness.tool_exec import _build_session_sandbox_spec

        spec = _build_session_sandbox_spec(session, tenant, owner)
        await sandbox_pool.ensure(owner, spec)

    return _ensure


async def _run_coding_agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    agent = arguments.get("agent", "")
    prompt = (arguments.get("prompt") or "").strip()
    if agent not in AGENT_TO_PROVIDER:
        return json.dumps(
            {"error": f"Unknown agent {agent!r}; use 'claude' or 'codex'."}
        )
    if not prompt:
        return json.dumps({"error": "prompt is required"})

    tenant = kwargs.get("tenant")
    session_id = kwargs.get("session_id")
    store = kwargs.get("session_store")
    sandbox_pool = kwargs.get("sandbox_pool")
    vault = kwargs.get("credential_vault")
    if tenant is None or tenant.user_id is None:
        return json.dumps({"error": "no end-user identity for coding-agent run"})
    if store is None or sandbox_pool is None:
        return json.dumps(
            {"error": "coding agents are not available on this deployment"}
        )
    if vault is None:
        return json.dumps({"error": "credential vault is not configured"})

    session = await store.get_session(UUID(str(session_id)))
    if session is None:
        return json.dumps({"error": "session not found"})

    from surogates.sandbox.pool import sandbox_session_key

    owner = sandbox_session_key(session)
    provider = AGENT_TO_PROVIDER[agent]

    async def _execute(name: str, input_json: str) -> str:
        return await sandbox_pool.execute(owner, name, input_json)

    outcome = await execute_coding_run(
        store=store, tenant=tenant, session=session,
        credentials=CodingAgentCredentials(vault),
        agent=agent, provider=provider, prompt=prompt,
        model=arguments.get("model"), effort=arguments.get("effort"),
        read_only=bool(arguments.get("read_only")),
        ensure_sandbox=_build_ensure(sandbox_pool, session, tenant, owner),
        execute=_execute,
        should_cancel=lambda: False,
    )

    if outcome.status == "not_connected":
        return json.dumps({
            "error": f"{agent} is not connected. The user must connect their "
                     f"plan in Settings -> Coding Agents before this can run.",
        })

    r = outcome.result
    return json.dumps({
        "final_message": r.final_message,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "error": r.error,
    })
