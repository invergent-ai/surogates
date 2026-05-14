"""Tests for the worker's channel-session token selection and the
tool-set filtering that pairs with it.

Two small module-level helpers (``_select_harness_token`` and
``_filter_effective_tools``) carry the new behaviour so the worker's
harness factory stays thin and the principal-aware logic is
independently testable.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from surogates.tenant.auth.jwt import decode_token
from surogates.tenant.context import TenantContext


# ---------------------------------------------------------------------------
# Context + session builders
# ---------------------------------------------------------------------------


def _tenant(*, user_id: UUID | None = None, tmp_path: Path) -> TenantContext:
    return TenantContext(
        org_id=uuid4(),
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(tmp_path),
        service_account_id=None,
        session_scope_id=None,
    )


def _session(*, channel: str, service_account_id: UUID | None = None):
    return SimpleNamespace(
        id=uuid4(),
        channel=channel,
        service_account_id=service_account_id,
    )


# ---------------------------------------------------------------------------
# _select_harness_token
# ---------------------------------------------------------------------------


class TestSelectHarnessToken:
    """The token selector returns the right JWT type for each principal."""

    def test_user_session_mints_access_token(self, tmp_path: Path):
        from surogates.orchestrator.worker import _select_harness_token

        user_id = uuid4()
        tenant = _tenant(user_id=user_id, tmp_path=tmp_path)
        session = _session(channel="web")

        token = _select_harness_token(
            tenant=tenant, session=session, agent_id="support-bot",
        )
        assert token is not None
        payload = decode_token(token)
        assert payload["type"] == "access"
        assert payload["user_id"] == str(user_id)

    def test_service_account_session_mints_sa_session_token(
        self, tmp_path: Path,
    ):
        from surogates.orchestrator.worker import _select_harness_token

        tenant = _tenant(tmp_path=tmp_path)
        sa_id = uuid4()
        session = _session(channel="api", service_account_id=sa_id)

        token = _select_harness_token(
            tenant=tenant, session=session, agent_id="support-bot",
        )
        assert token is not None
        payload = decode_token(token)
        assert payload["type"] == "service_account_session"
        assert payload["service_account_id"] == str(sa_id)
        assert payload["session_id"] == str(session.id)

    def test_website_session_mints_channel_session_token(
        self, tmp_path: Path,
    ):
        from surogates.orchestrator.worker import _select_harness_token

        tenant = _tenant(tmp_path=tmp_path)
        session = _session(channel="website")

        token = _select_harness_token(
            tenant=tenant, session=session, agent_id="support-bot",
        )
        assert token is not None
        payload = decode_token(token)
        assert payload["type"] == "channel_session"
        assert payload["agent_id"] == "support-bot"
        assert payload["session_id"] == str(session.id)
        assert payload["channel"] == "website"
        assert "user_id" not in payload
        assert "service_account_id" not in payload

    def test_user_session_preserves_tenant_permissions(
        self, tmp_path: Path,
    ):
        """``create_access_token`` carries the tenant's permissions
        when present (the fallback default kicks in only when empty)."""
        from surogates.orchestrator.worker import _select_harness_token

        user_id = uuid4()
        tenant = TenantContext(
            org_id=uuid4(),
            user_id=user_id,
            org_config={},
            user_preferences={},
            permissions=frozenset({"sessions:read", "skills:read"}),
            asset_root=str(tmp_path),
            service_account_id=None,
            session_scope_id=None,
        )
        token = _select_harness_token(
            tenant=tenant, session=_session(channel="web"),
            agent_id="x",
        )
        payload = decode_token(token)
        assert set(payload["permissions"]) == {
            "sessions:read", "skills:read",
        }

    def test_unknown_channel_no_token(self, tmp_path: Path):
        from surogates.orchestrator.worker import _select_harness_token

        tenant = _tenant(tmp_path=tmp_path)
        session = _session(channel="not-a-shipped-channel")

        token = _select_harness_token(
            tenant=tenant, session=session, agent_id="support-bot",
        )
        assert token is None


# ---------------------------------------------------------------------------
# _filter_effective_tools
# ---------------------------------------------------------------------------


class TestFilterEffectiveTools:
    """Principal-aware filtering of the LLM-visible tool set."""

    def test_create_artifact_kept_for_website(self, tmp_path: Path):
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="website"),
            use_api_for_harness_tools=True,
        )
        assert "create_artifact" in result

    def test_memory_dropped_for_website(self, tmp_path: Path):
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="website"),
            use_api_for_harness_tools=True,
        )
        assert "memory" not in result

    def test_skill_manage_dropped_for_website(self, tmp_path: Path):
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="website"),
            use_api_for_harness_tools=True,
        )
        assert "skill_manage" not in result

    def test_skills_list_kept_for_website(self, tmp_path: Path):
        """Read-only ``skills_list`` survives — slash-skill flow needs it."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="website"),
            use_api_for_harness_tools=True,
        )
        assert "skills_list" in result

    def test_memory_kept_for_user_session(self, tmp_path: Path):
        """Regression: user sessions retain memory/skill_manage/create_artifact."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(user_id=uuid4(), tmp_path=tmp_path),
            session=_session(channel="web"),
            use_api_for_harness_tools=True,
        )
        assert "memory" in result
        assert "skill_manage" in result
        assert "create_artifact" in result

    def test_memory_kept_for_service_account_session(self, tmp_path: Path):
        """Regression: SA sessions retain memory/skill_manage."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "memory", "skill_manage"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="api", service_account_id=uuid4()),
            use_api_for_harness_tools=True,
        )
        assert "memory" in result
        assert "skill_manage" in result
        assert "create_artifact" in result

    def test_create_artifact_dropped_when_api_disabled(self, tmp_path: Path):
        """Regression: ``use_api_for_harness_tools=False`` still drops it."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(user_id=uuid4(), tmp_path=tmp_path),
            session=_session(channel="web"),
            use_api_for_harness_tools=False,
        )
        assert "create_artifact" not in result

    def test_create_artifact_dropped_for_unknown_channel_no_principal(
        self, tmp_path: Path,
    ):
        """No principal AND non-anonymous-channel → no api_client → drop."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {"create_artifact", "skills_list"}
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="not-a-real-channel"),
            use_api_for_harness_tools=True,
        )
        assert "create_artifact" not in result

    def test_unrelated_tools_pass_through(self, tmp_path: Path):
        """Filter must not touch tools it isn't responsible for."""
        from surogates.orchestrator.worker import _filter_effective_tools

        tools = {
            "terminal", "read_file", "write_file", "web_search",
            "create_artifact", "memory",
        }
        result = _filter_effective_tools(
            tools=tools,
            tenant=_tenant(tmp_path=tmp_path),
            session=_session(channel="website"),
            use_api_for_harness_tools=True,
        )
        # Channel session, api_client present → drop memory, keep everything else.
        assert "terminal" in result
        assert "read_file" in result
        assert "write_file" in result
        assert "web_search" in result
        assert "create_artifact" in result
        assert "memory" not in result


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------


class TestAnonymousChannelsConstant:
    def test_includes_website(self):
        from surogates.orchestrator.worker import ANONYMOUS_CHANNELS

        assert "website" in ANONYMOUS_CHANNELS

    def test_is_immutable(self):
        """``frozenset`` so a future contributor can't accidentally
        add channels in a way that escapes audit."""
        from surogates.orchestrator.worker import ANONYMOUS_CHANNELS

        assert isinstance(ANONYMOUS_CHANNELS, frozenset)
