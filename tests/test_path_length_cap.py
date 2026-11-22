"""A path over the limit is refused before anything resolves it, on every route that takes one."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.coding_agents import pod_runner
from surogates.governance.policy import GovernanceGate
from surogates.harness.subdirectory_hints import SubdirectoryHintTracker
from surogates.harness.tool_exec import execute_single_tool
from surogates.sandbox import executor_server
from surogates.tools.builtin import skills as skills_mod
from surogates.tools.builtin.skills import _skill_view_handler
from surogates.tools.loader import SKILL_SOURCE_PLATFORM, SkillDef
from surogates.tools.utils.workspace_sandbox import (
    MAX_PATH_CHARS,
    PATH_TOO_LONG,
    WorkspaceSandboxError,
    path_too_long,
    shown_path,
    validate_path,
    validate_workdir,
)
from surogates.tools.workspace_io import LocalWorkspaceIO
from surogates.tools.workspace_io.storage import StorageWorkspaceIO
from tests.test_skill_view_hides_graph_file import _make_tenant, _stub_load_all_skills

AT_LIMIT = "a" * MAX_PATH_CHARS
OVER = "a" * (MAX_PATH_CHARS + 1)
MANY_SEGMENTS = "a/" * 100_000 + "x.txt"


@pytest.fixture
def resolved(monkeypatch):
    """The length of every path anything resolved."""
    seen: list[int] = []
    real_realpath, real_resolve = os.path.realpath, Path.resolve

    def realpath(path, *args, **kwargs):
        seen.append(len(os.fspath(path)))
        return real_realpath(path, *args, **kwargs)

    def resolve(self, *args, **kwargs):
        seen.append(len(str(self)))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    monkeypatch.setattr(Path, "resolve", resolve)
    return seen


def test_the_limit_is_exact() -> None:
    assert not path_too_long(AT_LIMIT)
    assert path_too_long(OVER)


def test_a_path_object_is_measured_too() -> None:
    assert not path_too_long(Path("a/b"))
    assert path_too_long(Path(OVER))
    assert shown_path(Path("a/b")) == "a/b"


@pytest.mark.parametrize("validate", [validate_path, validate_workdir])
def test_the_validators_take_the_limit_and_refuse_one_more(tmp_path, validate) -> None:
    assert validate(str(tmp_path), AT_LIMIT) == str(tmp_path.resolve() / AT_LIMIT)
    with pytest.raises(WorkspaceSandboxError, match="too long"):
        validate(str(tmp_path), OVER)


def test_the_gate_refuses_before_it_resolves(tmp_path, resolved) -> None:
    resolved.clear()
    decision = GovernanceGate().check("read_file", {"path": MANY_SEGMENTS}, workspace_path=str(tmp_path))
    assert not decision.allowed
    assert "too long" in decision.reason and "a/a/" not in decision.reason
    assert all(length <= MAX_PATH_CHARS for length in resolved)


def test_the_gate_lets_a_path_at_the_limit_reach_its_check(tmp_path) -> None:
    decision = GovernanceGate().check("read_file", {"path": AT_LIMIT}, workspace_path=str(tmp_path))
    assert "too long" not in (decision.reason or "")


@pytest.mark.asyncio
async def test_check_write_refuses_before_it_resolves(tmp_path, resolved) -> None:
    resolved.clear()
    assert await LocalWorkspaceIO(str(tmp_path)).check_write(MANY_SEGMENTS) == PATH_TOO_LONG
    assert all(length <= MAX_PATH_CHARS for length in resolved)


@pytest.mark.asyncio
async def test_check_write_takes_a_path_at_the_limit(tmp_path) -> None:
    assert await LocalWorkspaceIO(str(tmp_path)).check_write(AT_LIMIT) != PATH_TOO_LONG


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [None, "workspace"])
async def test_resolve_refuses_with_or_without_a_workspace(tmp_path, resolved, root) -> None:
    files = LocalWorkspaceIO(str(tmp_path) if root else None)
    resolved.clear()
    with pytest.raises(WorkspaceSandboxError, match="too long"):
        await files.resolve(MANY_SEGMENTS)
    assert all(length <= MAX_PATH_CHARS for length in resolved)


@pytest.mark.asyncio
async def test_the_storage_workspace_refuses_without_repeating_it() -> None:
    files = StorageWorkspaceIO(SimpleNamespace(), bucket="bucket", prefix="p/")
    with pytest.raises(WorkspaceSandboxError) as refused:
        await files.resolve(OVER)
    assert OVER not in str(refused.value)
    with pytest.raises(WorkspaceSandboxError) as traversal:
        await files.resolve("/" + "b" * 3000)
    assert "b" * 300 not in str(traversal.value)


def test_hints_leave_a_long_candidate_unresolved(tmp_path, resolved) -> None:
    tracker = SubdirectoryHintTracker(str(tmp_path))
    resolved.clear()
    candidates: set[str] = set()
    tracker._add_path_candidate(MANY_SEGMENTS, candidates)
    assert candidates == set() and resolved == []


def test_a_refusal_never_repeats_a_long_path(tmp_path) -> None:
    long = "/" + "b" * 4000
    with pytest.raises(WorkspaceSandboxError) as refused:
        validate_path(str(tmp_path), long)
    assert "b" * 300 not in str(refused.value)
    decision = GovernanceGate().check("read_file", {"path": long}, workspace_path=str(tmp_path))
    assert "b" * 300 not in decision.reason


@pytest.mark.asyncio
async def test_a_write_refusal_never_repeats_a_long_path(tmp_path) -> None:
    said = await LocalWorkspaceIO(None).check_write("/etc/" + "b" * 3000)
    assert said is not None and "b" * 300 not in said


def test_a_long_working_folder_is_refused_in_one_sentence(tmp_path) -> None:
    with pytest.raises(WorkspaceSandboxError) as refused:
        LocalWorkspaceIO(str(tmp_path))._workdir(OVER)
    assert str(refused.value) == PATH_TOO_LONG


@pytest.mark.asyncio
async def test_a_skill_file_path_is_capped(tmp_path, monkeypatch, resolved) -> None:
    skill_dir = tmp_path / "skills" / "proc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: proc\ndescription: d\n---\nbody\n", encoding="utf-8")
    skill = SkillDef(name="proc", description="d", content="body", source=SKILL_SOURCE_PLATFORM)
    _stub_load_all_skills(monkeypatch, [skill])
    monkeypatch.setattr(skills_mod, "_resolve_skill_dir", lambda *a, **kw: skill_dir)
    resolved.clear()
    answer = json.loads(await _skill_view_handler(
        {"name": "proc", "file_path": MANY_SEGMENTS}, tenant=_make_tenant(tmp_path),
    ))
    assert answer["error"] == PATH_TOO_LONG
    assert all(length <= MAX_PATH_CHARS for length in resolved)


@pytest.mark.asyncio
async def test_a_skill_file_path_over_the_cap_is_not_sent_to_the_api(tmp_path, monkeypatch) -> None:
    skill = SkillDef(name="proc", description="d", content="body", source=SKILL_SOURCE_PLATFORM)
    _stub_load_all_skills(monkeypatch, [skill])
    api_client = SimpleNamespace(view_skill=AsyncMock(side_effect=AssertionError("sent to the API")))
    answer = json.loads(await _skill_view_handler(
        {"name": "proc", "file_path": OVER}, tenant=_make_tenant(tmp_path), api_client=api_client,
    ))
    assert answer["error"] == PATH_TOO_LONG
    api_client.view_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_skill_file_name_the_file_system_cannot_hold_is_answered_short(tmp_path, monkeypatch) -> None:
    skill_dir = tmp_path / "skills" / "proc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: proc\ndescription: d\n---\nbody\n", encoding="utf-8")
    skill = SkillDef(name="proc", description="d", content="body", source=SKILL_SOURCE_PLATFORM)
    _stub_load_all_skills(monkeypatch, [skill])
    monkeypatch.setattr(skills_mod, "_resolve_skill_dir", lambda *a, **kw: skill_dir)
    answer = json.loads(await _skill_view_handler(
        {"name": "proc", "file_path": AT_LIMIT}, tenant=_make_tenant(tmp_path),
    ))
    assert answer["success"] is False and len(answer["error"]) < 400


def test_the_pods_error_text_never_repeats_a_long_path(tmp_path) -> None:
    (tmp_path / "file").write_text("x")
    answer = json.loads(executor_server._run_file(
        {"action": "write", "path": "file/" + "b" * 3000, "content_b64": "eA=="}, str(tmp_path),
    ))
    assert "b" * 300 not in answer["error"]


def test_a_coding_runs_long_working_folder_is_refused(tmp_path, monkeypatch) -> None:
    def never(*_args, **_kwargs):
        raise AssertionError("the run was started")

    monkeypatch.setattr(pod_runner.subprocess, "Popen", never)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    answer = pod_runner.launch(
        {
            "run_id": "r1", "argv": ["true"], "stdin": None, "workdir": str(workspace / OVER),
            "env": {"WORKSPACE_DIR": str(workspace)},
        },
        base=str(tmp_path / "runs"),
    )
    assert answer["ok"] is False and answer["error"] == PATH_TOO_LONG and answer["cleaned"] is True


def test_the_pod_refuses_a_long_file_path(tmp_path, resolved) -> None:
    resolved.clear()
    answer = json.loads(executor_server._run_file({"action": "read", "path": MANY_SEGMENTS}, str(tmp_path)))
    assert answer == {"error": PATH_TOO_LONG} and resolved == []


def test_the_pod_does_not_note_a_read_of_a_long_path(tmp_path, resolved) -> None:
    resolved.clear()
    executor_server._record_read("read_file", {"path": MANY_SEGMENTS}, str(tmp_path), json.dumps({"content": "x"}))
    assert resolved == []


@pytest.mark.asyncio
async def test_a_long_path_does_not_hold_the_event_loop_through_a_tool_call(tmp_path, resolved) -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(1, 500))
    store.consume_approval_grant = AsyncMock(return_value=(False, []))
    tools = SimpleNamespace(get=lambda _name: None, dispatch=AsyncMock(return_value="ran"))
    session = SimpleNamespace(id=uuid4(), config={"workspace_path": str(tmp_path)}, agent_id="a", org_id=uuid4())
    gaps: list[float] = []

    async def heartbeat() -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.01)
            gaps.append(time.monotonic() - started)

    beating = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    resolved.clear()
    try:
        await execute_single_tool(
            {"id": "call_1", "function": {"name": "read_file", "arguments": json.dumps({"path": MANY_SEGMENTS})}},
            session=session,
            lease=SimpleNamespace(lease_token=uuid4()),
            store=store,
            tools=tools,
            tenant=SimpleNamespace(org_id=session.org_id),
            hint_tracker=SubdirectoryHintTracker(str(tmp_path)),
        )
        await asyncio.sleep(0.02)
    finally:
        beating.cancel()
    tools.dispatch.assert_not_awaited()
    assert all(length <= MAX_PATH_CHARS for length in resolved)
    assert max(gaps) < 1.0
