"""A session with no terminal in its sandbox is not told to run a skill's scripts there.

EXTENV-1 (toolathlon read, 2026-11-19): every tool of those sessions was the environment's (its own terminal
in its own container); ``skill_view("xlsx")`` still staged the skill into the surogates sandbox and said "files
live at /workspace/.skills/xlsx/ ... python scripts/recalc.py", where none of the session's tools reach.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.api.routes.skills import read_skill_file, view_skill
from surogates.tools.builtin.skills import SCRIPTS_OUT_OF_REACH, _skill_view_handler
from surogates.tools.skill_visibility import SkillScope, skill_scope
from tests.test_skills_config import STORAGE_BUCKET, _make_tenant, _staging_request, _xlsx_system_bundle


async def test_the_api_stages_nothing_when_asked_not_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    request = _staging_request(tmp_path, monkeypatch, system_bundle=_xlsx_system_bundle())
    detail = await view_skill(name="xlsx", request=request, tenant=_make_tenant(), session_id=uuid4(), stage=False)
    assert detail.linked_files == ["scripts/recalc.py"]
    assert detail.staged_at is None and "Skill staging" not in (detail.content or "")
    assert await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="") == []


async def test_a_binary_file_read_unstaged_is_not_staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundle = _xlsx_system_bundle()
    bundle._files["xlsx/assets/template.xlsx"] = b"\xff\xfe"
    request = _staging_request(tmp_path, monkeypatch, system_bundle=bundle)
    out = await read_skill_file(
        name="xlsx", path="assets/template.xlsx", request=request, tenant=_make_tenant(), session_id=uuid4(),
        stage=False,
    )
    assert out.get("staged_at") is None and out.get("binary") is True
    assert await request.app.state.storage.list_keys(STORAGE_BUCKET, prefix="") == []


def _api(linked: list[str]):
    view = AsyncMock(return_value=json.dumps({"success": True, "name": "xlsx", "content": "Run scripts/recalc.py",
                                               "linked_files": linked}))
    return SimpleNamespace(view_skill=view)


async def test_without_a_terminal_the_view_says_the_scripts_cannot_run_and_nothing_is_staged():
    token = skill_scope.set(SkillScope(can_run_commands=False))
    try:
        api = _api(["scripts/recalc.py"])
        out = json.loads(await _skill_view_handler({"name": "xlsx"}, api_client=api, session_config={}))
        assert api.view_skill.await_args.kwargs == {"stage": False}
        assert out["content"].startswith(SCRIPTS_OUT_OF_REACH)
        # A skill without scripts (references, a licence) reads as before: its files are read with skill_view.
        for linked in ([], ["references/api.md", "LICENSE.txt"]):
            api = _api(linked)
            out = json.loads(await _skill_view_handler({"name": "xlsx"}, api_client=api, session_config={}))
            assert out["content"] == "Run scripts/recalc.py"
        # A file of the skill is read unstaged, as written.
        api = _api(["scripts/recalc.py"])
        await _skill_view_handler({"name": "xlsx", "file_path": "scripts/recalc.py"}, api_client=api, session_config={})
        assert api.view_skill.await_args.args == ("xlsx", "scripts/recalc.py")
        assert api.view_skill.await_args.kwargs == {"stage": False}
    finally:
        skill_scope.reset(token)


async def test_without_a_terminal_an_expert_is_still_refused():
    """An expert's text is its own model's system prompt: never handed to the executor, terminal or not."""
    token = skill_scope.set(SkillScope(can_run_commands=False))
    try:
        api = SimpleNamespace(view_skill=AsyncMock(return_value=json.dumps(
            {"success": True, "name": "advisor", "type": "expert", "content": "You are a strategic advisor.",
             "linked_files": ["scripts/x.py"]},
        )))
        out = json.loads(await _skill_view_handler({"name": "advisor"}, api_client=api, session_config={}))
        assert out["success"] is False and "You are a strategic advisor" not in json.dumps(out)
    finally:
        skill_scope.reset(token)


async def test_on_a_computer_without_a_terminal_nothing_is_put_in_the_folder():
    from tests.test_skills_config import ON_A_COMPUTER

    token = skill_scope.set(SkillScope(can_run_commands=False))
    try:
        api = _api(["scripts/recalc.py"])
        out = json.loads(await _skill_view_handler(
            {"name": "xlsx"}, api_client=api, session_config={**ON_A_COMPUTER, "workspace_path": "/home/u/proj"},
        ))
        assert out["content"].startswith(SCRIPTS_OUT_OF_REACH) and "staged_at" not in out
    finally:
        skill_scope.reset(token)


async def test_with_a_terminal_nothing_changes():
    api = _api(["scripts/recalc.py"])
    out = json.loads(await _skill_view_handler({"name": "xlsx"}, api_client=api, session_config={}))
    assert api.view_skill.await_args.kwargs == {}
    assert out["content"] == "Run scripts/recalc.py"


async def test_the_client_asks_the_api_not_to_stage():
    from surogates.harness.api_client import HarnessAPIClient

    client = HarnessAPIClient.__new__(HarnessAPIClient)
    client._session_id = "s-1"
    client._get = AsyncMock(return_value={"name": "xlsx"})
    await client.view_skill("xlsx", stage=False)
    assert client._get.await_args.kwargs["params"] == {"session_id": "s-1", "stage": "false"}
    await client.view_skill("xlsx")
    assert client._get.await_args.kwargs["params"] == {"session_id": "s-1"}
    await client.view_skill("xlsx", "assets/t.xlsx", stage=False)
    assert client._get.await_args.kwargs["params"] == {"session_id": "s-1", "path": "assets/t.xlsx", "stage": "false"}


def test_a_session_without_a_terminal_is_scoped_so():
    from surogates.governance.policy import GovernanceGate
    from tests.test_skills_name_only_offered_tools import HOX, _harness

    harness, session = _harness(HOX)  # terminal and run_coding_agent among its excluded tools
    harness._scope_skills(session)
    assert skill_scope.get().can_run_commands is False
    harness, session = _harness({})
    harness._scope_skills(session)
    assert skill_scope.get().can_run_commands is True
    # Denied by the policy, as by the template.
    harness, session = _harness({})
    harness._governance_gate = GovernanceGate(denied_tools={"terminal", "run_coding_agent"})
    harness._scope_skills(session)
    assert skill_scope.get().can_run_commands is False
    # A coding agent works in the sandbox: the scripts can run.
    harness, session = _harness({"excluded_tools": ["terminal"]})
    harness._scope_skills(session)
    assert skill_scope.get().can_run_commands is True
