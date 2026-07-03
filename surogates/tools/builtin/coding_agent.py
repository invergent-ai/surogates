"""Built-in ``run_coding_agent`` tool — run Claude Code / Codex from the LLM.

Worker-local (mirrors ``consult_expert``).  The LLM hands a task to an external
coding agent that checks out one of the agent's configured git repositories on
a fresh branch, implements the task, then commits, pushes, and opens a pull
request for review.  The streamed run renders as a CodeRunBlock and the final
message (with the PR URL) is returned to the calling LLM.

Required kwargs (injected by the harness dispatch): ``tenant``, ``session_id``,
``session_store``, ``sandbox_pool``, ``credential_vault``, ``session_config``
(the wake-local config carrying the agent's ``repos``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from surogates.coding_agents.command import AGENT_TO_PROVIDER
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.pr_workflow import augment_prompt, parse_pr_url
from surogates.coding_agents.repo_checkout import fix_branch_name
from surogates.coding_agents.repo_resolve import resolve_git_pat, select_repo
from surogates.coding_agents.run_core import execute_coding_run
from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.utils.interrupt import is_interrupted

logger = logging.getLogger(__name__)

_SCHEMA = ToolSchema(
    name="run_coding_agent",
    description=(
        "Run an external coding agent (Claude Code or Codex) on one of the "
        "agent's configured git repositories: it checks out the repo on a fresh "
        "branch, implements the task, then commits, pushes, and opens a pull "
        "request for review. Use 'claude' to implement or edit code, 'codex' to "
        "review and run tests. Pass 'repo' to choose when several are "
        "configured. One run is one shot — it cannot pause to ask questions. "
        "Returns the PR URL when one is opened."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "codex"]},
            "prompt": {
                "type": "string",
                "description": "The task for the coding agent.",
            },
            "repo": {
                "type": "string",
                "description": (
                    "Which configured repository to work on (name or URL). "
                    "Optional when exactly one repo is configured."
                ),
            },
            "model": {"type": "string"},
            "effort": {
                "type": "string",
                "enum": ["low", "medium", "high", "xhigh"],
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
    if tenant is None or (
        getattr(tenant, "user_id", None) is None
        and getattr(tenant, "service_account_id", None) is None
    ):
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

    # Resolve the target repo from the wake-local config (the fresh session row
    # from the DB does not carry the runtime-projected repos overlay).
    effective_config = kwargs.get("session_config") or session.config or {}
    repos = effective_config.get("repos") or []
    repo = select_repo(repos, arguments.get("repo"))
    if repo is None:
        return json.dumps({
            "error": "Configure a repository in the agent's Tools tab first.",
            "code": "repo_not_configured",
        })

    pat = await resolve_git_pat(
        vault, org_id=tenant.org_id,
        user_id=getattr(tenant, "user_id", None),
        service_account_id=getattr(tenant, "service_account_id", None),
    )
    if not pat:
        return json.dumps({
            "error": "Connect a GitHub token in the agent's Tools tab first.",
            "code": "git_pat_not_connected",
        })

    from surogates.sandbox.pool import sandbox_session_key

    owner = sandbox_session_key(session)
    provider = AGENT_TO_PROVIDER[agent]

    async def _execute(name: str, input_json: str) -> str:
        return await sandbox_pool.execute(owner, name, input_json)

    # The caller computes the branch so the augmented prompt and the checkout
    # use the same name.
    branch = fix_branch_name(prompt, now=time.time())
    augmented = augment_prompt(
        prompt, branch=branch, default_branch=repo["default_branch"],
    )

    outcome = await execute_coding_run(
        store=store, tenant=tenant, session=session,
        credentials=CodingAgentCredentials(vault),
        agent=agent, provider=provider, prompt=augmented,
        model=arguments.get("model"), effort=arguments.get("effort"),
        read_only=False,
        ensure_sandbox=_build_ensure(sandbox_pool, session, tenant, owner),
        execute=_execute,
        # Poll the global interrupt so a session/mission cancel stops the run
        # promptly (kills the pod-side CLI) instead of polling to completion.
        should_cancel=lambda: is_interrupted(),
        repo=repo, git_pat=pat, branch=branch,
        summary_client=kwargs.get("summary_llm_client"),
        summary_model=kwargs.get("summary_model"),
    )

    if outcome.status == "not_connected":
        return json.dumps({
            "error": f"{agent} is not connected. The user must connect their "
                     f"plan in Settings -> Coding Agents before this can run.",
        })

    r = outcome.result
    text = r.final_message if r else ""
    url = parse_pr_url(text)
    result: dict[str, Any] = {
        "final_message": text,
        "input_tokens": r.input_tokens if r else 0,
        "output_tokens": r.output_tokens if r else 0,
        "error": r.error if r else None,
        "repo": repo["url"],
        "branch": outcome.branch,
        "checkout_dir": outcome.checkout_dir,
        "pr_url": url,
    }
    if url is None:
        result["note"] = "No PR URL detected in the coding agent final message."
    return json.dumps(result)
