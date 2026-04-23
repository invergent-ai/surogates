"""Tests for platform hints, model guidance, developer role, and conditional skill filtering."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from surogates.harness.llm_call import (
    DEVELOPER_ROLE_MODELS,
    _should_use_developer_role,
    apply_developer_role,
)
from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import default_library
from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader, SkillDef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant(tmp_path: Path, **overrides) -> TenantContext:
    defaults = dict(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={
            "agent_name": "TestBot",
            "personality": "You are helpful.",
            "default_model": "gpt-4o",
        },
        user_preferences={},
        permissions=frozenset({"read"}),
        asset_root=str(tmp_path),
    )
    defaults.update(overrides)
    return TenantContext(**defaults)


def _make_session(channel: str = "web", workspace_path: str | None = None):
    """Create a mock Session object."""
    from unittest.mock import MagicMock

    session = MagicMock()
    session.channel = channel
    session.config = {}
    if workspace_path:
        session.config["workspace_path"] = workspace_path
    return session


# ---------------------------------------------------------------------------
# Platform hints in PromptBuilder
# ---------------------------------------------------------------------------


class TestPlatformHints:
    def test_web_hint(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        session = _make_session(channel="whatsapp")
        builder = PromptBuilder(tenant, session=session)
        prompt = builder.build()
        assert "WhatsApp" in prompt

    def test_slack_hint(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        session = _make_session(channel="slack")
        builder = PromptBuilder(tenant, session=session)
        prompt = builder.build()
        assert "Slack" in prompt

    def test_telegram_hint(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        session = _make_session(channel="telegram")
        builder = PromptBuilder(tenant, session=session)
        prompt = builder.build()
        assert "Telegram" in prompt

    def test_unknown_channel_no_hint(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        session = _make_session(channel="custom_channel")
        builder = PromptBuilder(tenant, session=session)
        prompt = builder.build()
        # Should still build successfully, just without a platform hint.
        assert "Platform" not in prompt or "Surogates" in prompt

    def test_no_session_no_hint(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        builder = PromptBuilder(tenant)
        prompt = builder.build()
        assert isinstance(prompt, str)

    def test_all_hints_are_strings(self):
        platforms = default_library().platforms()
        assert platforms, "at least one platform hint should be discoverable"
        for channel, hint in platforms.items():
            assert isinstance(channel, str)
            assert isinstance(hint, str)
            assert len(hint) > 0


# ---------------------------------------------------------------------------
# Model-specific guidance
# ---------------------------------------------------------------------------


class TestModelGuidance:
    def test_gpt_model_gets_enforcement(self, tmp_path: Path):
        tenant = _make_tenant(
            tmp_path,
            org_config={"agent_name": "Bot", "default_model": "gpt-4o"},
        )
        builder = PromptBuilder(tenant)
        prompt = builder.build()
        assert "Execution discipline" in prompt or "tool_persistence" in prompt

    def test_gemini_model_gets_operational(self, tmp_path: Path):
        tenant = _make_tenant(
            tmp_path,
            org_config={"agent_name": "Bot", "default_model": "gemini-pro"},
        )
        builder = PromptBuilder(tenant)
        prompt = builder.build()
        assert "operational directives" in prompt.lower() or "Absolute paths" in prompt

    def test_claude_model_no_enforcement(self, tmp_path: Path):
        tenant = _make_tenant(
            tmp_path,
            org_config={"agent_name": "Bot", "default_model": "claude-3.5-sonnet"},
        )
        builder = PromptBuilder(tenant)
        prompt = builder.build()
        # Claude should NOT get tool-use enforcement.
        assert "Execution Standards" not in prompt
        assert "Operational Requirements" not in prompt

    def test_grok_gets_enforcement(self, tmp_path: Path):
        tenant = _make_tenant(
            tmp_path,
            org_config={"agent_name": "Bot", "default_model": "grok-3"},
        )
        builder = PromptBuilder(tenant, available_tools={"terminal", "web_search"})
        prompt = builder.build()
        assert "Tool-use enforcement" in prompt

    def test_kimi_gets_enforcement(self, tmp_path: Path):
        # Added after session cbf414ac…e1362a1 where Kimi promised to
        # "offer an HTML artifact" and ended the turn without a tool call.
        tenant = _make_tenant(
            tmp_path,
            org_config={"agent_name": "Bot", "default_model": "moonshotai/kimi-k2.6"},
        )
        builder = PromptBuilder(tenant, available_tools={"terminal", "create_artifact"})
        prompt = builder.build()
        assert "Tool-use enforcement" in prompt


# ---------------------------------------------------------------------------
# Developer role routing
# ---------------------------------------------------------------------------


class TestDeveloperRole:
    def test_gpt5_uses_developer(self):
        assert _should_use_developer_role("gpt-5-turbo") is True

    def test_codex_uses_developer(self):
        assert _should_use_developer_role("codex-mini") is True

    def test_o3_uses_developer(self):
        assert _should_use_developer_role("o3-mini") is True

    def test_o4_uses_developer(self):
        assert _should_use_developer_role("o4-preview") is True

    def test_gpt4_does_not_use_developer(self):
        assert _should_use_developer_role("gpt-4o") is False

    def test_claude_does_not_use_developer(self):
        assert _should_use_developer_role("claude-3.5-sonnet") is False

    def test_apply_developer_role(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        result = apply_developer_role(messages, "gpt-5-turbo")
        assert result[0]["role"] == "developer"
        assert result[1]["role"] == "user"
        # Original should be unmodified.
        assert messages[0]["role"] == "system"

    def test_apply_developer_role_no_op_for_gpt4(self):
        messages = [
            {"role": "system", "content": "System prompt"},
        ]
        result = apply_developer_role(messages, "gpt-4o")
        assert result[0]["role"] == "system"


# ---------------------------------------------------------------------------
# Conditional skill filtering
# ---------------------------------------------------------------------------


class TestConditionalSkillFiltering:
    def _make_skill(self, name: str, **kwargs) -> SkillDef:
        defaults = {
            "description": f"Skill {name}",
            "content": "# Instructions",
            "source": "user",
        }
        defaults.update(kwargs)
        return SkillDef(name=name, **defaults)

    def test_no_conditions_passes(self):
        loader = ResourceLoader()
        skills = [self._make_skill("basic")]
        result = loader.filter_skills(skills, {"file_read", "terminal"})
        assert len(result) == 1

    def test_fallback_skipped_when_tools_available(self):
        loader = ResourceLoader()
        skill = self._make_skill(
            "manual-search",
            fallback_for_tools=["web_search"],
        )
        result = loader.filter_skills([skill], {"web_search", "terminal"})
        assert len(result) == 0

    def test_fallback_shown_when_tools_missing(self):
        loader = ResourceLoader()
        skill = self._make_skill(
            "manual-search",
            fallback_for_tools=["web_search"],
        )
        result = loader.filter_skills([skill], {"terminal"})
        assert len(result) == 1

    def test_requires_tools_present(self):
        loader = ResourceLoader()
        skill = self._make_skill(
            "docker-ops",
            requires_tools=["terminal"],
        )
        result = loader.filter_skills([skill], {"terminal", "file_read"})
        assert len(result) == 1

    def test_requires_tools_missing(self):
        loader = ResourceLoader()
        skill = self._make_skill(
            "docker-ops",
            requires_tools=["terminal", "browser_navigate"],
        )
        result = loader.filter_skills([skill], {"terminal"})
        assert len(result) == 0

    def test_mixed_filtering(self):
        loader = ResourceLoader()
        skills = [
            self._make_skill("always"),
            self._make_skill("fallback", fallback_for_tools=["web_search"]),
            self._make_skill("needs-browser", requires_tools=["browser_navigate"]),
        ]
        available = {"web_search", "terminal"}
        result = loader.filter_skills(skills, available)
        names = [s.name for s in result]
        assert "always" in names
        assert "fallback" not in names  # web_search is available
        assert "needs-browser" not in names  # browser_navigate is missing
