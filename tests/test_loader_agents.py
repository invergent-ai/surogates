"""Tests for sub-agent loading in surogates.tools.loader.ResourceLoader."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.loader import (
    AGENT_SOURCE_ORG,
    AGENT_SOURCE_PLATFORM,
    AGENT_SOURCE_USER,
    AgentDef,
    ResourceLoader,
)


def _make_tenant(asset_root: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


def _loader(tmp_path: Path, agents_dir: Path | None = None) -> ResourceLoader:
    return ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills_unused"),
        platform_mcp_dir=str(tmp_path / "mcp_unused"),
        platform_agents_dir=(
            str(agents_dir) if agents_dir is not None
            else str(tmp_path / "agents_unused")
        ),
    )


# =========================================================================
# _load_agents_from_dir
# =========================================================================


class TestLoadAgentsFromDir:
    """AGENT.md file parsing with YAML frontmatter."""

    def test_parses_yaml_frontmatter(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "code-reviewer").mkdir(parents=True)
        (agents_dir / "code-reviewer" / "AGENT.md").write_text(
            "---\n"
            "name: code-reviewer\n"
            "description: Reviews code for quality\n"
            "tools: [read_file, search_files]\n"
            "disallowed_tools: [write_file, patch]\n"
            "model: claude-sonnet-4-6\n"
            "max_iterations: 20\n"
            "policy_profile: read_only\n"
            "---\n"
            "You are a senior code reviewer.  Focus on security, "
            "correctness, and maintainability.\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")

        assert len(agents) == 1
        a = agents[0]
        assert a.name == "code-reviewer"
        assert a.description == "Reviews code for quality"
        assert a.tools == ["read_file", "search_files"]
        assert a.disallowed_tools == ["write_file", "patch"]
        assert a.model == "claude-sonnet-4-6"
        assert a.max_iterations == 20
        assert a.policy_profile == "read_only"
        assert a.source == "platform"
        assert a.enabled is True
        assert "senior code reviewer" in a.system_prompt
        # Frontmatter block must be stripped from the system prompt body.
        assert "---" not in a.system_prompt

    def test_comma_separated_tools_string(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "researcher").mkdir(parents=True)
        (agents_dir / "researcher" / "AGENT.md").write_text(
            "---\n"
            "name: researcher\n"
            "description: research agent\n"
            "tools: read_file, search_files, web_search\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert len(agents) == 1
        assert agents[0].tools == ["read_file", "search_files", "web_search"]

    def test_fallback_name_from_directory(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "my-agent").mkdir(parents=True)
        (agents_dir / "my-agent" / "AGENT.md").write_text(
            "No frontmatter here, just content.\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert len(agents) == 1
        assert agents[0].name == "my-agent"
        assert agents[0].tools is None
        assert agents[0].disallowed_tools is None

    def test_flat_layout(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "db-reader.md").write_text(
            "---\nname: db-reader\ndescription: read-only\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert len(agents) == 1
        assert agents[0].name == "db-reader"

    def test_category_from_nested_path(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "research" / "hypothesis-generator").mkdir(parents=True)
        (agents_dir / "research" / "hypothesis-generator" / "AGENT.md").write_text(
            "---\nname: hypothesis-generator\ndescription: ideas\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert len(agents) == 1
        assert agents[0].category == "research"

    def test_enabled_false_preserved(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "disabled").mkdir(parents=True)
        (agents_dir / "disabled" / "AGENT.md").write_text(
            "---\nname: disabled\ndescription: off\nenabled: false\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert len(agents) == 1
        assert agents[0].enabled is False

    def test_max_iterations_coerced_to_int(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "bounded").mkdir(parents=True)
        (agents_dir / "bounded" / "AGENT.md").write_text(
            "---\nname: bounded\ndescription: x\nmax_iterations: 5\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert agents[0].max_iterations == 5

    def test_max_iterations_invalid_becomes_none(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "bad").mkdir(parents=True)
        (agents_dir / "bad" / "AGENT.md").write_text(
            "---\nname: bad\ndescription: x\nmax_iterations: not-a-number\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert agents[0].max_iterations is None

    def test_excluded_dirs_skipped(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / ".git" / "hooks").mkdir(parents=True)
        (agents_dir / ".git" / "hooks" / "AGENT.md").write_text(
            "---\nname: should-not-load\ndescription: x\n---\nBody\n",
            encoding="utf-8",
        )
        (agents_dir / "valid").mkdir()
        (agents_dir / "valid" / "AGENT.md").write_text(
            "---\nname: valid\ndescription: x\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        names = {a.name for a in agents}
        assert "valid" in names
        assert "should-not-load" not in names

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        empty_dir = tmp_path / "empty_agents"
        empty_dir.mkdir()
        loader = _loader(tmp_path)
        assert loader._load_agents_from_dir(str(empty_dir), "platform") == []

    def test_empty_tools_does_not_become_literal_none_entry(self, tmp_path: Path):
        """YAML ``tools:`` with no value used to string-coerce to ``"None"``
        and produce a bogus ``["None"]`` tool list.  Preserving native
        YAML types drops the null instead."""
        agents_dir = tmp_path / "agents"
        (agents_dir / "null-tools").mkdir(parents=True)
        (agents_dir / "null-tools" / "AGENT.md").write_text(
            "---\nname: null-tools\ndescription: d\ntools:\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert agents[0].tools is None

    def test_enabled_false_as_native_yaml_bool(self, tmp_path: Path):
        """YAML ``enabled: false`` round-trips as a native bool, not the
        string ``"False"``.  ``_build_agent_def`` accepts either form."""
        agents_dir = tmp_path / "agents"
        (agents_dir / "off").mkdir(parents=True)
        (agents_dir / "off" / "AGENT.md").write_text(
            "---\nname: off\ndescription: d\nenabled: false\n---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        agents = loader._load_agents_from_dir(str(agents_dir), "platform")
        assert agents[0].enabled is False

    def test_unknown_frontmatter_keys_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """Typos like 'disallow_tools' should be logged so admins see the
        feedback instead of silently running an unconstrained agent."""
        import logging

        agents_dir = tmp_path / "agents"
        (agents_dir / "typo").mkdir(parents=True)
        (agents_dir / "typo" / "AGENT.md").write_text(
            "---\n"
            "name: typo\n"
            "description: d\n"
            "disallow_tools: [write_file]\n"  # missing 'ed'
            "max_iteration: 5\n"               # missing 's'
            "---\nBody\n",
            encoding="utf-8",
        )

        loader = _loader(tmp_path)
        with caplog.at_level(logging.WARNING, logger="surogates.tools.loader"):
            agents = loader._load_agents_from_dir(str(agents_dir), "platform")

        assert len(agents) == 1
        # Both typoed keys surfaced in the warning.
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "disallow_tools" in messages
        assert "max_iteration" in messages
        # Unconstrained agent: disallowed_tools is still None.
        assert agents[0].disallowed_tools is None
        assert agents[0].max_iterations is None

    def test_nonexistent_directory_returns_empty(self, tmp_path: Path):
        loader = _loader(tmp_path)
        assert loader._load_agents_from_dir(
            str(tmp_path / "nope"), "platform",
        ) == []


# =========================================================================
# resolve_platform_agent_dir
# =========================================================================


class TestResolvePlatformAgentDir:

    def test_direct_layout(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "foo").mkdir(parents=True)
        (agents_dir / "foo" / "AGENT.md").write_text("x", encoding="utf-8")

        loader = _loader(tmp_path, agents_dir=agents_dir)
        result = loader.resolve_platform_agent_dir("foo")
        assert result == agents_dir / "foo"

    def test_nested_layout(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        (agents_dir / "research" / "bar").mkdir(parents=True)
        (agents_dir / "research" / "bar" / "AGENT.md").write_text("x", encoding="utf-8")

        loader = _loader(tmp_path, agents_dir=agents_dir)
        result = loader.resolve_platform_agent_dir("bar")
        assert result == agents_dir / "research" / "bar"

    def test_missing_returns_none(self, tmp_path: Path):
        loader = _loader(tmp_path)
        assert loader.resolve_platform_agent_dir("nope") is None


# =========================================================================
# Merge precedence (filesystem layers only -- DB covered in integration)
# =========================================================================


class TestLoadAgentsMergePrecedence:
    """User > org > platform precedence for the filesystem-only code path."""

    @pytest.mark.asyncio
    async def test_user_overrides_org_and_platform(self, tmp_path: Path):
        org_id = "00000000-0000-0000-0000-000000000001"
        user_id = "00000000-0000-0000-0000-000000000002"

        # Platform agent
        platform_dir = tmp_path / "platform_agents"
        (platform_dir / "shared").mkdir(parents=True)
        (platform_dir / "shared" / "AGENT.md").write_text(
            "---\nname: shared\ndescription: platform version\n---\nPlatform body\n",
            encoding="utf-8",
        )

        # Org agent (same name)
        org_dir = tmp_path / "assets" / org_id / "shared" / "agents" / "shared"
        org_dir.mkdir(parents=True)
        (org_dir / "AGENT.md").write_text(
            "---\nname: shared\ndescription: org version\n---\nOrg body\n",
            encoding="utf-8",
        )

        # User agent (same name)
        user_dir = (
            tmp_path / "assets" / org_id / "users" / user_id / "agents" / "shared"
        )
        user_dir.mkdir(parents=True)
        (user_dir / "AGENT.md").write_text(
            "---\nname: shared\ndescription: user version\n---\nUser body\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(tmp_path / "mcp"),
            platform_agents_dir=str(platform_dir),
        )

        tenant = _make_tenant(str(tmp_path / "assets"))
        agents = await loader.load_agents(tenant)

        shared = [a for a in agents if a.name == "shared"]
        assert len(shared) == 1
        assert shared[0].description == "user version"
        assert shared[0].source == AGENT_SOURCE_USER

    @pytest.mark.asyncio
    async def test_platform_only_when_no_tenant_files(self, tmp_path: Path):
        platform_dir = tmp_path / "platform_agents"
        (platform_dir / "only-me").mkdir(parents=True)
        (platform_dir / "only-me" / "AGENT.md").write_text(
            "---\nname: only-me\ndescription: platform\n---\nBody\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(tmp_path / "mcp"),
            platform_agents_dir=str(platform_dir),
        )

        tenant = _make_tenant(str(tmp_path / "assets"))
        agents = await loader.load_agents(tenant)

        assert len(agents) == 1
        assert agents[0].name == "only-me"
        assert agents[0].source == AGENT_SOURCE_PLATFORM

    @pytest.mark.asyncio
    async def test_org_overrides_platform_when_no_user(self, tmp_path: Path):
        org_id = "00000000-0000-0000-0000-000000000001"

        platform_dir = tmp_path / "platform_agents"
        (platform_dir / "x").mkdir(parents=True)
        (platform_dir / "x" / "AGENT.md").write_text(
            "---\nname: x\ndescription: platform\n---\nP\n",
            encoding="utf-8",
        )

        org_dir = tmp_path / "assets" / org_id / "shared" / "agents" / "x"
        org_dir.mkdir(parents=True)
        (org_dir / "AGENT.md").write_text(
            "---\nname: x\ndescription: org\n---\nO\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(tmp_path / "mcp"),
            platform_agents_dir=str(platform_dir),
        )

        tenant = _make_tenant(str(tmp_path / "assets"))
        agents = await loader.load_agents(tenant)

        assert len(agents) == 1
        assert agents[0].description == "org"
        assert agents[0].source == AGENT_SOURCE_ORG


# =========================================================================
# AgentDef defaults
# =========================================================================


class TestAgentDefDefaults:

    def test_minimal_def(self):
        a = AgentDef(
            name="m", description="d", system_prompt="body", source="platform",
        )
        assert a.enabled is True
        assert a.tools is None
        assert a.disallowed_tools is None
        assert a.model is None
        assert a.max_iterations is None
        assert a.policy_profile is None
        assert a.category is None
        assert a.tags is None
