"""``/code`` slash-command handler mixed into AgentHarness.

Implements help/status/login/logout plus the live ``/code claude|codex`` run
path: it loads the user's connected credential, launches the vendor CLI in the
session sandbox via :mod:`surogates.coding_agents.runner`, streams coalesced
progress, and records the final result.

Security model: the credential is the user's own plan token, injected only
into the spawned CLI's process environment inside that user's own per-session
sandbox pod, cleaned up after the run, and never written to any event payload
or log.  Conflicting provider env vars are scrubbed before launch.
"""

from __future__ import annotations

import logging

from surogates.coding_agents.command import parse_code_command
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.messages import (
    render_connect_first,
    render_help,
    render_login_instructions,
    render_status,
)
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

_NO_VAULT = "Credential vault is not configured on this deployment."
_NO_SANDBOX = "Coding agents need a sandbox, which isn't available on this deployment."


class CodeCommandMixin:
    """Provides ``_handle_code_command``.  Expects the host to define
    ``self._store`` (SessionStore), ``self._tenant`` (TenantContext),
    ``self._credential_vault`` (CredentialVault | None), and
    ``self._sandbox_pool`` (SandboxPool | None)."""

    async def _handle_code_command(self, session, content, lease, all_events=None) -> None:
        cmd = parse_code_command(content)
        if cmd is None:  # defensive — dispatch only calls us for /code
            return

        # The run path emits its own STARTED/PROGRESS/RESULT events.
        if cmd.action == "run" and not cmd.error:
            await self._run_code_agent(session, cmd, lease, all_events)
            return

        if cmd.action == "help":
            message = (f"{cmd.error}\n\n" if cmd.error else "") + render_help()
        elif cmd.action == "status":
            message = await self._render_code_status()
        elif cmd.action == "login":
            message = cmd.error or render_login_instructions(cmd.agent)
        elif cmd.action == "logout":
            message = cmd.error or await self._logout_code_provider(
                cmd.provider, cmd.agent,
            )
        elif cmd.action == "run":  # only reached when cmd.error is set
            message = cmd.error
        else:
            message = render_help()

        await self._emit_code_message(session, message, lease)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _emit_code_message(self, session, message: str, lease) -> None:
        response_event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=response_event_id,
            lease_token=lease.lease_token,
        )

    def _code_credentials(self) -> CodingAgentCredentials | None:
        if getattr(self, "_credential_vault", None) is None:
            return None
        return CodingAgentCredentials(self._credential_vault)

    async def _render_code_status(self) -> str:
        creds = self._code_credentials()
        if creds is None:
            return _NO_VAULT
        statuses = await creds.statuses(
            org_id=self._tenant.org_id, user_id=self._tenant.user_id,
        )
        return render_status(statuses)

    async def _logout_code_provider(self, provider: str, agent: str) -> str:
        creds = self._code_credentials()
        if creds is None:
            return _NO_VAULT
        removed = await creds.delete(
            org_id=self._tenant.org_id,
            user_id=self._tenant.user_id,
            provider=provider,
        )
        return f"Disconnected {agent}." if removed else f"{agent} was not connected."

    # ------------------------------------------------------------------
    # Run path
    # ------------------------------------------------------------------

    async def _run_code_agent(self, session, cmd, lease, all_events) -> None:
        from surogates.coding_agents.run_core import execute_coding_run

        creds = self._code_credentials()
        if creds is None:
            await self._emit_code_message(session, _NO_VAULT, lease)
            return
        sandbox_pool = getattr(self, "_sandbox_pool", None)
        if sandbox_pool is None:
            await self._emit_code_message(session, _NO_SANDBOX, lease)
            return

        # Idempotency: a crash-recovery re-wake replays the same user.message;
        # if a run for this source event already started, do not relaunch.
        source_event_id = _latest_user_event_id(all_events)
        if source_event_id is not None and _code_run_already_started(
            all_events, source_event_id,
        ):
            return

        from surogates.sandbox.pool import sandbox_session_key

        sandbox_owner = sandbox_session_key(session)

        async def _ensure() -> None:
            await self._ensure_code_sandbox(session, sandbox_owner)

        async def _execute(name: str, input_json: str) -> str:
            return await sandbox_pool.execute(sandbox_owner, name, input_json)

        try:
            outcome = await execute_coding_run(
                store=self._store, tenant=self._tenant, session=session,
                credentials=creds, agent=cmd.agent, provider=cmd.provider,
                prompt=cmd.prompt, model=cmd.flags.get("model"),
                effort=cmd.flags.get("effort"),
                read_only=cmd.flags.get("allow") == "read-only",
                ensure_sandbox=_ensure, execute=_execute,
                should_cancel=lambda: bool(
                    getattr(self, "_interrupt_requested", False)
                ),
                started_metadata={"source_event_id": source_event_id},
            )
        except Exception as exc:  # provisioning/build failure — report cleanly
            logger.warning("/code run failed: %s", exc)
            await self._emit_code_message(
                session, f"Could not run {cmd.agent}: {exc}", lease,
            )
            return

        if outcome.status == "not_connected":
            await self._emit_code_message(
                session, render_connect_first(cmd.agent), lease,
            )
            return

        # The core already emitted CODE_RUN_RESULT — advance the cursor through
        # it so this terminal slash turn is durably processed.
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=outcome.result_event_id,
            lease_token=lease.lease_token,
        )

    async def _ensure_code_sandbox(self, session, sandbox_owner: str) -> None:
        from surogates.harness.tool_exec import _build_session_sandbox_spec

        spec = _build_session_sandbox_spec(session, self._tenant, sandbox_owner)
        await self._sandbox_pool.ensure(sandbox_owner, spec)


def _latest_user_event_id(all_events) -> int | None:
    if not all_events:
        return None
    latest: int | None = None
    for event in all_events:
        etype = getattr(event, "type", None)
        etype = etype.value if hasattr(etype, "value") else str(etype)
        eid = getattr(event, "id", None)
        if etype == EventType.USER_MESSAGE.value and eid is not None:
            if latest is None or eid > latest:
                latest = eid
    return latest


def _code_run_already_started(all_events, source_event_id: int) -> bool:
    """True if a CODE_RUN_STARTED already references *source_event_id*."""
    if not all_events:
        return False
    for event in all_events:
        etype = getattr(event, "type", None)
        etype = etype.value if hasattr(etype, "value") else str(etype)
        if etype == EventType.CODE_RUN_STARTED.value:
            data = getattr(event, "data", {}) or {}
            if data.get("source_event_id") == source_event_id:
                return True
    return False
