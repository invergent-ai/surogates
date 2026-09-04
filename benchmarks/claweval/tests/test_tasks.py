from dataclasses import dataclass, field

import pytest

from claweval_bench.tasks import _skip_reason, select_ids


@dataclass
class _Prompt:
    text: str = "do it"
    language: str = "en"
    attachments: list = field(default_factory=list)


@dataclass
class _UserAgent:
    enabled: bool = False


@dataclass
class _Task:
    task_id: str = "T001_x"
    prompt: _Prompt = field(default_factory=_Prompt)
    tools: list = field(default_factory=lambda: [{"name": "t"}])
    tool_endpoints: list = field(default_factory=lambda: [{"tool_name": "t"}])
    user_agent: _UserAgent = field(default_factory=_UserAgent)
    sandbox_files: list = field(default_factory=list)
    sandbox_grader_files: list = field(default_factory=list)
    env_snapshot_files: list = field(default_factory=list)
    env_snapshot_commands: list = field(default_factory=list)


def test_mock_service_task_is_eligible():
    assert _skip_reason(_Task()) is None


def test_multi_turn_is_skipped():
    assert "multi-turn" in _skip_reason(_Task(user_agent=_UserAgent(enabled=True)))


def test_sandbox_fixtures_are_skipped():
    assert "sandbox" in _skip_reason(_Task(sandbox_files=["a.xlsx"]))


def test_snapshots_are_skipped():
    assert "snapshot" in _skip_reason(_Task(env_snapshot_files=["out.txt"]))


def test_attachments_are_skipped():
    task = _Task(prompt=_Prompt(attachments=["video.mp4"]))
    assert "media" in _skip_reason(task)


def test_toolless_task_is_skipped():
    assert "no mock-service tools" in _skip_reason(_Task(tools=[]))


def test_select_ids_accepts_prefixes():
    tasks = [_Task(task_id="T001_a"), _Task(task_id="T002_b")]
    assert [t.task_id for t in select_ids(tasks, "T002")] == ["T002_b"]


def test_select_ids_raises_on_unmatched():
    with pytest.raises(SystemExit):
        select_ids([_Task()], "T999")
