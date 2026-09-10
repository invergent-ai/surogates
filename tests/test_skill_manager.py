"""Skill management through the builtin tool handler."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID


from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skill_manager import (
    _skill_manage_handler,
)


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
