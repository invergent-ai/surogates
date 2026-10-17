"""Run ids and task selection."""
import pytest

from obench.cli import build_parser, invalid_rollouts, invalidate, next_run_id, rollout_dirs, select_tasks
from obench.tasks import TASKS


def test_next_run_id_sequences_per_prefix(tmp_path):
    assert next_run_id(tmp_path, "full") == "full-001"
    (tmp_path / "full-002").mkdir()
    (tmp_path / "full-custom").mkdir()
    (tmp_path / "pilot-001").mkdir()
    assert next_run_id(tmp_path, "full") == "full-003"
    assert next_run_id(tmp_path, "pilot") == "pilot-002"


def test_select_by_ids_kind_mode_pilot_limit():
    assert [t.id for t in select_tasks(TASKS, ids="pdf-fill,docx-bold")] == ["pdf-fill", "docx-bold"]
    assert {t.kind for t in select_tasks(TASKS, kind="xlsx,pdf")} == {"xlsx", "pdf"}
    assert {t.mode for t in select_tasks(TASKS, mode="edit")} == {"edit"}
    assert len(select_tasks(TASKS, pilot=True)) == 6
    assert len(select_tasks(TASKS, kind="pptx", limit=2)) == 2


def test_unknown_ids_and_kinds_are_errors():
    with pytest.raises(SystemExit, match="no task matches"):
        select_tasks(TASKS, ids="docx-bold,nope")
    with pytest.raises(SystemExit, match="unknown kind"):
        select_tasks(TASKS, kind="odt")
    with pytest.raises(SystemExit, match="no task selected"):
        select_tasks(TASKS, ids="pdf-fill", kind="docx")


def test_run_flags():
    args = build_parser().parse_args(["run", "--runs", "3", "--kind", "docx", "--mode", "create"])
    assert (args.runs, args.kind, args.mode, args.pilot) == (3, "docx", "create", False)


def test_invalidate_sets_a_rollout_aside_for_a_rerun(tmp_path):
    d = tmp_path / "docx-bold" / "r1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "checks.json").write_text("{}")
    assert len(rollout_dirs(tmp_path)) == 1
    invalidate(tmp_path, "docx-bold", "DNS down")
    assert rollout_dirs(tmp_path) == []
    assert not d.exists()
    [(name, why)] = invalid_rollouts(tmp_path)
    assert name == "docx-bold/invalid-r1-1" and why.endswith("DNS down")
