"""Skill management through the builtin tool handler."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from surogates.devices.sandbox import enter_device_session, leave_device_session
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


class _Api:
    """The api client's skill calls, recorded."""

    def __init__(self) -> None:
        self.called: list[str] = []

    def __getattr__(self, method: str):
        async def call(*_args, **_kwargs) -> str:
            self.called.append(method)
            return json.dumps({"success": True})

        return call


_CHANGES = [
    ({"action": "create", "name": "deck", "content": VALID_SKILL_CONTENT}, "create_skill"),
    ({"action": "edit", "name": "deck", "content": VALID_SKILL_CONTENT}, "edit_skill"),
    ({"action": "patch", "name": "deck", "old_string": "a", "new_string": "b"}, "patch_skill"),
    ({"action": "delete", "name": "deck"}, "delete_skill"),
    ({"action": "write_file", "name": "deck", "file_path": "scripts/build.py", "file_content": "x"}, "write_skill_file"),
    ({"action": "remove_file", "name": "deck", "file_path": "scripts/build.py"}, "remove_skill_file"),
]
_ON_A_FOLDER = {"execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-0000000000aa"}}


@pytest.mark.parametrize(("arguments", "method"), _CHANGES)
async def test_a_chat_on_a_local_folder_changes_no_skill_and_a_cloud_chat_does(arguments, method):
    # skill_view stages a skill's files into the folder unasked: the model may not change what it stages.
    api = _Api()
    refused = json.loads(await _skill_manage_handler(arguments, api_client=api, session_config=_ON_A_FOLDER))
    assert refused == {
        "success": False,
        "error": "Skills can't be changed from a chat on a local folder. Change them in the web client.",
    }
    # An expert's tool loop passes no session config: the wake's mark says it.
    token = enter_device_session(SimpleNamespace(id=uuid4(), parent_id=None, config=_ON_A_FOLDER))
    try:
        assert json.loads(await _skill_manage_handler(arguments, api_client=api)) == refused
    finally:
        leave_device_session(token)
    assert api.called == []
    assert json.loads(await _skill_manage_handler(arguments, api_client=api, session_config={}))["success"] is True
    assert api.called == [method]
