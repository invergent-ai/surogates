"""Dependency-injected core that runs one coding agent and emits events.

Shared by the ``/code`` slash handler and the ``run_coding_agent`` tool so the
two never diverge.  Callers supply the side-effecting collaborators (sandbox
exec, sandbox-ensure, interrupt check); this module owns the credential ->
invocation -> launch/poll/stream -> result + codex write-back sequence and the
CODE_RUN_* event emission.  Credentials are never placed in an event payload.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from surogates.coding_agents.agents import CodeResult, build_invocation
from surogates.coding_agents.credentials import (
    CodingAgentCredentials,
    CredentialBundle,
)
from surogates.coding_agents.run_progress import (
    CHANNEL_UPDATE_INTERVAL,
    render_code_run_ack,
    render_code_run_update,
)
from surogates.coding_agents.runner import run_code_agent
from surogates.session.events import EventType


@dataclass
class CodingRunOutcome:
    status: str  # "ok" | "not_connected"
    result: CodeResult | None = None
    result_event_id: int | None = None
    branch: str | None = None
    checkout_dir: str | None = None


def credential_env(bundle: CredentialBundle) -> tuple[dict[str, str], str | None]:
    """Map a credential bundle to a launch env + optional codex auth.json.

    Only the minimal credential for the chosen provider/mode is emitted;
    nothing else is added to the child environment.
    """
    if bundle.provider == "anthropic":
        if bundle.auth_mode == "oauth":
            return {"CLAUDE_CODE_OAUTH_TOKEN": bundle.oauth_token or ""}, None
        return {"ANTHROPIC_API_KEY": bundle.api_key or ""}, None
    # openai / codex
    if bundle.auth_mode == "oauth":
        return {}, json.dumps(bundle.auth_json or {})
    return {"OPENAI_API_KEY": bundle.api_key or ""}, None


async def execute_coding_run(
    *,
    store,
    tenant,
    session,
    credentials: CodingAgentCredentials,
    agent: str,
    provider: str,
    prompt: str,
    model: str | None,
    effort: str | None,
    read_only: bool,
    ensure_sandbox: Callable[[], Awaitable[None]],
    execute: Callable[[str, str], Awaitable[str]],
    should_cancel: Callable[[], bool],
    started_metadata: dict | None = None,
    repo: dict | None = None,
    git_pat: str | None = None,
    branch: str | None = None,
    now: float | None = None,
) -> CodingRunOutcome:
    """Run one coding agent end to end, emitting CODE_RUN_* events.

    Returns ``not_connected`` (without emitting anything) when the user has
    no stored credential for *provider*; otherwise drives the full run and
    returns the parsed result plus the CODE_RUN_RESULT event id so callers
    can advance their cursor through it.
    """
    sa_id = getattr(tenant, "service_account_id", None)
    bundle = await credentials.load(
        org_id=tenant.org_id, provider=provider,
        user_id=tenant.user_id, service_account_id=sa_id,
    )
    if bundle is None:
        return CodingRunOutcome(status="not_connected")

    invocation = build_invocation(
        agent, prompt, model=model, effort=effort, read_only=read_only,
    )
    env, codex_auth_json = credential_env(bundle)

    run_id = uuid4().hex
    started_data = {
        "run_id": run_id, "agent": agent, "provider": provider, "prompt": prompt,
    }
    if started_metadata:
        # e.g. the slash path passes {"source_event_id": ...} for crash-recovery
        # idempotency; the tool path passes nothing (tool-call replay covers it).
        started_data.update(started_metadata)
    await store.emit_event(session.id, EventType.CODE_RUN_STARTED, started_data)

    await ensure_sandbox()

    # Optional repo checkout: clone the configured repo into the workspace on a
    # fresh branch (auth via a git credential helper reading $GH_TOKEN — the PAT
    # never enters argv/logs) and run the CLI inside that checkout so it can
    # commit, push, and open a PR.
    extra_env: dict[str, str] | None = None
    workdir: str | None = None
    checkout_dir: str | None = None
    if repo and git_pat:
        from surogates.coding_agents.repo_checkout import (
            clone_script,
            fix_branch_name,
            git_auth_env,
            repo_dir_name,
        )

        run_now = now if now is not None else time.time()
        # Honor a caller-computed branch (so the tool can augment the prompt
        # with the same name); otherwise derive it from the prompt.
        branch = branch or fix_branch_name(prompt, now=run_now)
        checkout_dir = f"/workspace/{repo_dir_name(repo['url'])}"
        git_env = git_auth_env(git_pat)
        checkout_payload = {
            "action": "checkout",
            "run_id": run_id,
            "command": clone_script(
                repo_url=repo["url"],
                default_branch=repo["default_branch"],
                dest=checkout_dir,
                branch=branch,
            ),
            "env": git_env,
        }
        checkout_result = json.loads(
            await execute("_code", json.dumps(checkout_payload)),
        )
        if not checkout_result.get("ok"):
            error = checkout_result.get("error") or "repo checkout failed"
            result_event_id = await store.emit_event(
                session.id,
                EventType.CODE_RUN_RESULT,
                {
                    "run_id": run_id, "agent": agent, "final_message": None,
                    "error": error, "input_tokens": 0, "output_tokens": 0,
                },
            )
            return CodingRunOutcome(
                status="ok",
                result=CodeResult(final_message="", error=error),
                result_event_id=result_event_id,
                branch=branch, checkout_dir=checkout_dir,
            )
        extra_env = git_env
        workdir = checkout_dir

    # Channel heartbeat: code-run PROGRESS renders live in the web UI but is
    # never delivered to channels, so a Slack/Telegram user sees nothing for
    # the minutes a run takes.  Post a throttled "still working" update for
    # channel sessions (web/api render progress themselves).
    wants_updates = getattr(session, "channel", "") not in ("web", "api")
    last_update = time.monotonic()
    if wants_updates:
        await store.emit_event(
            session.id,
            EventType.CODE_RUN_CHANNEL_UPDATE,
            {"run_id": run_id, "agent": agent, "text": render_code_run_ack(agent, repo)},
        )

    async def _emit_progress(chunk: str) -> None:
        nonlocal last_update
        await store.emit_event(
            session.id,
            EventType.CODE_RUN_PROGRESS,
            {"run_id": run_id, "agent": agent, "chunk": chunk},
        )
        if wants_updates and time.monotonic() - last_update >= CHANNEL_UPDATE_INTERVAL:
            last_update = time.monotonic()
            await store.emit_event(
                session.id,
                EventType.CODE_RUN_CHANNEL_UPDATE,
                {
                    "run_id": run_id, "agent": agent,
                    "text": render_code_run_update(repo, chunk),
                },
            )

    result = await run_code_agent(
        run_id=run_id,
        agent=agent,
        invocation=invocation,
        env=env,
        codex_auth_json=codex_auth_json,
        execute=execute,
        emit_progress=_emit_progress,
        should_cancel=should_cancel,
        sleep=asyncio.sleep,
        extra_env=extra_env,
        workdir=workdir,
    )

    # Codex refreshes its auth.json in-pod; persist the new copy so the vault
    # stays fresh and the user isn't forced to re-paste (best-effort).
    if provider == "openai" and result.updated_codex_auth_json:
        try:
            parsed = json.loads(result.updated_codex_auth_json)
            if isinstance(parsed, dict):
                await credentials.store(
                    org_id=tenant.org_id,
                    user_id=tenant.user_id, service_account_id=sa_id,
                    bundle=CredentialBundle(
                        provider="openai", auth_mode="oauth", auth_json=parsed,
                    ),
                )
        except (json.JSONDecodeError, TypeError):
            pass

    result_event_id = await store.emit_event(
        session.id,
        EventType.CODE_RUN_RESULT,
        {
            "run_id": run_id, "agent": agent,
            "final_message": result.final_message, "error": result.error,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        },
    )
    return CodingRunOutcome(
        status="ok", result=result, result_event_id=result_event_id,
        branch=branch, checkout_dir=checkout_dir,
    )
