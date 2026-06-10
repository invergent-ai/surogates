"""``/code`` slash-command handler mixed into AgentHarness.

v1 (auth foundation) implements help/status/login/logout and stubs the
run path; execution lands in a later plan.
"""

from __future__ import annotations

from surogates.coding_agents.command import parse_code_command
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.messages import (
    render_help,
    render_login_instructions,
    render_status,
)
from surogates.session.events import EventType

_NO_VAULT = "Credential vault is not configured on this deployment."


class CodeCommandMixin:
    """Provides ``_handle_code_command``.  Expects the host to define
    ``self._store`` (SessionStore), ``self._tenant`` (TenantContext), and
    ``self._credential_vault`` (CredentialVault | None)."""

    async def _handle_code_command(self, session, content, lease) -> None:
        cmd = parse_code_command(content)
        if cmd is None:  # defensive — dispatch only calls us for /code
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
        elif cmd.action == "run":
            message = cmd.error or (
                f"Running coding agents isn't available yet. Connect now with "
                f"`/code login {cmd.agent}` — execution ships in a later release."
            )
        else:
            message = render_help()

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
