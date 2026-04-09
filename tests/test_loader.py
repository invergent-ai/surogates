"""Tests for surogates.tools.loader.ResourceLoader."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader, SkillDef, MCPServerDef


def _make_tenant(asset_root: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


# =========================================================================
# load_skills_from_dir (via _load_skills_from_dir)
# =========================================================================


class TestLoadSkillsFromDir:
    """Skill file parsing with YAML frontmatter."""

    def test_parses_yaml_frontmatter(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "my_skill.md").write_text(
            "---\nname: my-skill\ndescription: Does something\n---\n"
            "# Skill body\nHello world\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(skills_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        # Use the private method directly for unit testing.
        skills = loader._load_skills_from_dir(str(skills_dir), "platform")
        assert len(skills) == 1
        assert skills[0].name == "my-skill"
        assert skills[0].description == "Does something"
        assert "Hello world" in skills[0].content

    def test_fallback_name_from_filename(self, tmp_path: Path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "auto_name.md").write_text(
            "No frontmatter here, just content.\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(skills_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        skills = loader._load_skills_from_dir(str(skills_dir), "platform")
        assert len(skills) == 1
        assert skills[0].name == "auto_name"

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()

        loader = ResourceLoader(
            platform_skills_dir=str(empty_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        skills = loader._load_skills_from_dir(str(empty_dir), "platform")
        assert skills == []

    def test_nonexistent_directory_returns_empty(self, tmp_path: Path):
        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "nope"),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )
        skills = loader._load_skills_from_dir(str(tmp_path / "nope"), "platform")
        assert skills == []


# =========================================================================
# Merge precedence
# =========================================================================


class TestMergePrecedence:
    """User > org > platform precedence."""

    def test_user_overrides_org_and_platform(self, tmp_path: Path):
        org_id = "00000000-0000-0000-0000-000000000001"
        user_id = "00000000-0000-0000-0000-000000000002"

        # Platform skill
        platform_dir = tmp_path / "platform_skills"
        platform_dir.mkdir()
        (platform_dir / "shared_skill.md").write_text(
            "---\nname: shared\ndescription: platform version\n---\nPlatform body\n",
            encoding="utf-8",
        )

        # Org skill (same name)
        org_skills_dir = tmp_path / "assets" / org_id / "shared" / "skills"
        org_skills_dir.mkdir(parents=True)
        (org_skills_dir / "shared_skill.md").write_text(
            "---\nname: shared\ndescription: org version\n---\nOrg body\n",
            encoding="utf-8",
        )

        # User skill (same name)
        user_skills_dir = tmp_path / "assets" / org_id / "users" / user_id / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "shared_skill.md").write_text(
            "---\nname: shared\ndescription: user version\n---\nUser body\n",
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(platform_dir),
            platform_mcp_dir=str(tmp_path / "mcp"),
        )

        tenant = _make_tenant(str(tmp_path / "assets"))
        skills = loader.load_skills(tenant)

        # Only one skill named "shared", and it should be the user version.
        shared_skills = [s for s in skills if s.name == "shared"]
        assert len(shared_skills) == 1
        assert shared_skills[0].description == "user version"
        assert shared_skills[0].source == "user"


# =========================================================================
# load_mcp_from_dir
# =========================================================================


class TestLoadMCPFromDir:
    """MCP server configuration parsing."""

    def test_parses_servers_json(self, tmp_path: Path):
        mcp_dir = tmp_path / "mcp"
        mcp_dir.mkdir()
        servers = {
            "github": {
                "transport": "stdio",
                "command": "github-mcp",
                "args": ["--token", "abc"],
            },
            "slack": {
                "transport": "http",
                "url": "http://localhost:8080",
            },
        }
        (mcp_dir / "servers.json").write_text(
            json.dumps(servers), encoding="utf-8"
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(mcp_dir),
        )
        defs = loader._load_mcp_from_dir(str(mcp_dir))
        assert len(defs) == 2

        names = {d.name for d in defs}
        assert "github" in names
        assert "slack" in names

        github = next(d for d in defs if d.name == "github")
        assert github.transport == "stdio"
        assert github.command == "github-mcp"
        assert github.args == ["--token", "abc"]

    def test_parses_individual_files(self, tmp_path: Path):
        mcp_dir = tmp_path / "mcp"
        mcp_dir.mkdir()
        (mcp_dir / "myserver.json").write_text(
            json.dumps({
                "name": "my_server",
                "transport": "stdio",
                "command": "my-mcp",
            }),
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(mcp_dir),
        )
        defs = loader._load_mcp_from_dir(str(mcp_dir))
        assert len(defs) == 1
        assert defs[0].name == "my_server"

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        mcp_dir = tmp_path / "empty_mcp"
        mcp_dir.mkdir()

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(mcp_dir),
        )
        defs = loader._load_mcp_from_dir(str(mcp_dir))
        assert defs == []

    def test_nonexistent_directory_returns_empty(self, tmp_path: Path):
        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(tmp_path / "nope"),
        )
        defs = loader._load_mcp_from_dir(str(tmp_path / "nope"))
        assert defs == []

    def test_mcp_server_def_defaults(self, tmp_path: Path):
        mcp_dir = tmp_path / "mcp"
        mcp_dir.mkdir()
        (mcp_dir / "minimal.json").write_text(
            json.dumps({"name": "minimal"}),
            encoding="utf-8",
        )

        loader = ResourceLoader(
            platform_skills_dir=str(tmp_path / "skills"),
            platform_mcp_dir=str(mcp_dir),
        )
        defs = loader._load_mcp_from_dir(str(mcp_dir))
        assert len(defs) == 1
        assert defs[0].transport == "stdio"
        assert defs[0].timeout == 120
        assert defs[0].env == {}
