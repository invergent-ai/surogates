"""Run-id sequencing and outcome round-tripping."""
from wsbench.cli import load_outcomes, next_run_id, save_outcomes
from wsbench.report import TaskOutcome


def test_next_run_id_starts_at_001(tmp_path):
    assert next_run_id(tmp_path, "dev") == "dev-001"
    assert next_run_id(tmp_path, "smoke") == "smoke-001"


def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "dev-001").mkdir()
    (tmp_path / "dev-002").mkdir()
    (tmp_path / "smoke-001").mkdir()
    # Smoke runs never shift dev numbering and vice versa.
    assert next_run_id(tmp_path, "dev") == "dev-003"
    assert next_run_id(tmp_path, "smoke") == "smoke-002"
    assert next_run_id(tmp_path, "holdout") == "holdout-001"


def test_next_run_id_ignores_stray_and_named_entries(tmp_path):
    (tmp_path / "dev-002").mkdir()
    (tmp_path / "dev-custom-name").mkdir()  # --run-id override
    (tmp_path / ".DS_Store").write_text("")  # stray file
    # Highest number wins, not entry count -- strays cannot shift ids.
    assert next_run_id(tmp_path, "dev") == "dev-003"


def test_outcomes_round_trip(tmp_path):
    outcomes = [TaskOutcome(
        task_id="3", persona="Researcher", difficulty="medium",
        total_rubrics=4, passed_rubrics=3,
        by_rubric_type={"Outcome Evaluation": [3, 4]},
        missing_outputs=["x.md"], terminal_status="completed",
    )]
    path = str(tmp_path / "outcomes.json")
    save_outcomes(path, outcomes)
    loaded = load_outcomes(path)
    assert loaded == outcomes
