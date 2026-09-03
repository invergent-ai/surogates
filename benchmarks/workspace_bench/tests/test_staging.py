"""Staging plans and the eligibility gate."""
import pytest

from wsbench import staging
from wsbench.dataset import ManifestFile, Task
from wsbench.staging import StagingError, eligibility, stage_plan


def _task(tmp_path, manifest, **overrides):
    task_dir = tmp_path / "task_lite_clean_en" / "3"
    (task_dir / "data").mkdir(parents=True, exist_ok=True)
    fields = dict(
        task_id="3",
        persona="Backend Developer",
        instruction="do it",
        difficulty="medium",
        output_files=("out.md",),
        rubrics=("was it done?",),
        rubric_types=("Basic Evaluation",),
        tested_capabilities=(),
        manifest=tuple(manifest),
        local_dir=str(task_dir),
    )
    fields.update(overrides)
    return Task(**fields)


def test_stage_plan_maps_stored_to_logical_names(tmp_path):
    task = _task(tmp_path, [ManifestFile("report.md", "data/ab12_report.md")])
    (tmp_path / "task_lite_clean_en" / "3" / "data" / "ab12_report.md").write_text("x")

    [staged] = stage_plan(task)
    assert staged.name == "report.md"
    assert staged.subdir == "workdir"
    assert staged.workspace_path == "workdir/report.md"
    assert staged.size == 1


def test_stage_plan_preserves_nested_logical_paths(tmp_path):
    task = _task(tmp_path, [ManifestFile("src/app.py", "data/cd34_app.py")])
    (tmp_path / "task_lite_clean_en" / "3" / "data" / "cd34_app.py").write_text("x")

    [staged] = stage_plan(task)
    assert staged.subdir == "workdir/src"
    assert staged.workspace_path == "workdir/src/app.py"


def test_missing_input_is_ineligible(tmp_path):
    task = _task(tmp_path, [ManifestFile("a.md", "data/none_a.md")])
    with pytest.raises(StagingError, match="missing from snapshot"):
        stage_plan(task)
    assert "missing from snapshot" in eligibility(task)


def test_oversize_input_is_ineligible(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "MAX_FILE_BYTES", 2)
    task = _task(tmp_path, [ManifestFile("a.md", "data/xx_a.md")])
    (tmp_path / "task_lite_clean_en" / "3" / "data" / "xx_a.md").write_text("abc")
    assert "upload cap" in eligibility(task)


def test_traversal_in_manifest_is_ineligible(tmp_path):
    task = _task(tmp_path, [ManifestFile("../evil.md", "data/xx_a.md")])
    (tmp_path / "task_lite_clean_en" / "3" / "data" / "xx_a.md").write_text("x")
    assert "traversal" in eligibility(task)


def test_empty_manifest_is_ineligible(tmp_path):
    task = _task(tmp_path, [])
    assert "empty data manifest" in eligibility(task)


def test_eligible_task_returns_none(tmp_path):
    task = _task(tmp_path, [ManifestFile("a.md", "data/xx_a.md")])
    (tmp_path / "task_lite_clean_en" / "3" / "data" / "xx_a.md").write_text("x")
    assert eligibility(task) is None
