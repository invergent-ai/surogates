"""Tests for surogates.tools.builtin.skill_manager."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skill_manager import (
    ALLOWED_SUBDIRS,
    _atomic_write,
    _create_skill,
    _delete_skill,
    _edit_skill,
    _find_skill,
    _patch_skill,
    _remove_file,
    _skill_manage_handler,
    _user_skills_dir,
    _validate_content_size,
    _validate_file_path,
    _validate_frontmatter,
    _validate_name,
    _write_file,
    register,
)
from surogates.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant(tmp_path: Path) -> TenantContext:
    org_id = UUID("00000000-0000-0000-0000-000000000001")
    user_id = UUID("00000000-0000-0000-0000-000000000002")
    # Pre-create the skills directory.
    skills_dir = tmp_path / str(org_id) / "users" / str(user_id) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset({"read", "write"}),
        asset_root=str(tmp_path),
    )


VALID_SKILL_CONTENT = """\
---
name: my-skill
description: A test skill
---
# My Skill

Do the thing step by step.
"""


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateName:
    def test_valid_names(self):
        assert _validate_name("my-skill") is None
        assert _validate_name("skill_v2") is None
        assert _validate_name("a") is None
        assert _validate_name("1abc") is None
        assert _validate_name("test.skill") is None

    def test_empty(self):
        assert _validate_name("") is not None

    def test_too_long(self):
        assert _validate_name("a" * 65) is not None

    def test_invalid_chars(self):
        assert _validate_name("My Skill") is not None
        assert _validate_name("UPPER") is not None
        assert _validate_name("has/slash") is not None

    def test_starts_with_special(self):
        assert _validate_name("-start") is not None
        assert _validate_name(".start") is not None


class TestValidateFrontmatter:
    def test_valid(self):
        assert _validate_frontmatter(VALID_SKILL_CONTENT) is None

    def test_empty_content(self):
        assert _validate_frontmatter("") is not None
        assert _validate_frontmatter("   ") is not None

    def test_missing_frontmatter(self):
        assert _validate_frontmatter("# Just markdown") is not None

    def test_unclosed_frontmatter(self):
        assert _validate_frontmatter("---\nname: x\n") is not None

    def test_missing_name(self):
        content = "---\ndescription: test\n---\nBody"
        assert "name" in _validate_frontmatter(content)

    def test_missing_description(self):
        content = "---\nname: test\n---\nBody"
        assert "description" in _validate_frontmatter(content)

    def test_no_body(self):
        content = "---\nname: test\ndescription: d\n---\n"
        assert _validate_frontmatter(content) is not None


class TestValidateContentSize:
    def test_within_limit(self):
        assert _validate_content_size("short") is None

    def test_exceeds_limit(self):
        big = "x" * 100_001
        assert _validate_content_size(big) is not None


class TestValidateFilePath:
    def test_valid_paths(self):
        assert _validate_file_path("references/doc.md") is None
        assert _validate_file_path("templates/tmpl.txt") is None
        assert _validate_file_path("scripts/run.sh") is None
        assert _validate_file_path("assets/logo.png") is None

    def test_empty(self):
        assert _validate_file_path("") is not None

    def test_path_traversal(self):
        assert _validate_file_path("../etc/passwd") is not None
        assert _validate_file_path("references/../../../etc") is not None

    def test_disallowed_subdir(self):
        assert _validate_file_path("private/secret.txt") is not None
        assert _validate_file_path("SKILL.md") is not None

    def test_directory_only(self):
        assert _validate_file_path("references") is not None


# ---------------------------------------------------------------------------
# Action tests
# ---------------------------------------------------------------------------


class TestCreateSkill:
    def test_create_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _create_skill("my-skill", VALID_SKILL_CONTENT, tenant)
        assert result["success"] is True
        assert "created" in result["message"]
        skill_md = _user_skills_dir(tenant) / "my-skill" / "SKILL.md"
        assert skill_md.read_text() == VALID_SKILL_CONTENT

    def test_create_with_category(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _create_skill("my-skill", VALID_SKILL_CONTENT, tenant, category="devops")
        assert result["success"] is True
        assert result.get("category") == "devops"

    def test_create_duplicate(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("dupe", VALID_SKILL_CONTENT, tenant)
        result = _create_skill("dupe", VALID_SKILL_CONTENT, tenant)
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_create_bad_name(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _create_skill("BAD NAME!", VALID_SKILL_CONTENT, tenant)
        assert result["success"] is False

    def test_create_bad_content(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _create_skill("good-name", "no frontmatter", tenant)
        assert result["success"] is False


class TestEditSkill:
    def test_edit_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("editable", VALID_SKILL_CONTENT, tenant)
        new_content = VALID_SKILL_CONTENT.replace("Do the thing", "Do something else")
        result = _edit_skill("editable", new_content, tenant)
        assert result["success"] is True

    def test_edit_not_found(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _edit_skill("nonexistent", VALID_SKILL_CONTENT, tenant)
        assert result["success"] is False
        assert "not found" in result["error"]


class TestPatchSkill:
    def test_patch_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("patchable", VALID_SKILL_CONTENT, tenant)
        result = _patch_skill("patchable", "Do the thing", "Do another thing", tenant)
        assert result["success"] is True
        assert "1 replacement" in result["message"]

    def test_patch_not_found_string(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("patchable2", VALID_SKILL_CONTENT, tenant)
        result = _patch_skill("patchable2", "NOT IN FILE", "something", tenant)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_patch_skill_not_found(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _patch_skill("missing", "old", "new", tenant)
        assert result["success"] is False


class TestDeleteSkill:
    def test_delete_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("deleteme", VALID_SKILL_CONTENT, tenant)
        result = _delete_skill("deleteme", tenant)
        assert result["success"] is True
        assert not (_user_skills_dir(tenant) / "deleteme").exists()

    def test_delete_not_found(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _delete_skill("ghost", tenant)
        assert result["success"] is False


class TestWriteFile:
    def test_write_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("with-files", VALID_SKILL_CONTENT, tenant)
        result = _write_file("with-files", "references/guide.md", "# Guide", tenant)
        assert result["success"] is True
        target = _user_skills_dir(tenant) / "with-files" / "references" / "guide.md"
        assert target.read_text() == "# Guide"

    def test_write_skill_not_found(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = _write_file("missing", "references/x.md", "content", tenant)
        assert result["success"] is False


class TestRemoveFile:
    def test_remove_success(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("removable", VALID_SKILL_CONTENT, tenant)
        _write_file("removable", "references/doc.md", "content", tenant)
        result = _remove_file("removable", "references/doc.md", tenant)
        assert result["success"] is True

    def test_remove_not_found(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        _create_skill("removable2", VALID_SKILL_CONTENT, tenant)
        result = _remove_file("removable2", "references/nope.md", tenant)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Handler / registration tests
# ---------------------------------------------------------------------------


class TestSkillManageHandler:
    async def test_handler_create(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = await _skill_manage_handler(
            {"action": "create", "name": "handler-test", "content": VALID_SKILL_CONTENT},
            tenant=tenant,
        )
        data = json.loads(result)
        assert data["success"] is True

    async def test_handler_no_tenant(self):
        result = await _skill_manage_handler(
            {"action": "create", "name": "x", "content": "y"},
        )
        data = json.loads(result)
        assert data["success"] is False
        assert "tenant" in data["error"].lower()

    async def test_handler_unknown_action(self, tmp_path: Path):
        tenant = _make_tenant(tmp_path)
        result = await _skill_manage_handler(
            {"action": "bogus", "name": "x"},
            tenant=tenant,
        )
        data = json.loads(result)
        assert data["success"] is False
        assert "Unknown action" in data["error"]


class TestRegistration:
    def test_register(self):
        reg = ToolRegistry()
        register(reg)
        assert reg.has("skill_manage")
        entry = reg.get("skill_manage")
        assert entry.toolset == "skills"


# ---------------------------------------------------------------------------
# Atomic write test
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "test.txt"
        _atomic_write(target, "hello")
        assert target.read_text() == "hello"

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c.txt"
        _atomic_write(target, "nested")
        assert target.read_text() == "nested"
