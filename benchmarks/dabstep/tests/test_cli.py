"""Run-id sequencing and outcome round-tripping."""
import json

from dabbench.cli import load_outcomes, next_run_id
from dabbench.report import TaskOutcome


def test_next_run_id_starts_at_001(tmp_path):
    assert next_run_id(tmp_path, "dev") == "dev-001"
    assert next_run_id(tmp_path, "smoke") == "smoke-001"


def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "dev-001").mkdir()
    (tmp_path / "smoke-004").mkdir()
    (tmp_path / "dev-custom").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    assert next_run_id(tmp_path, "dev") == "dev-002"
    assert next_run_id(tmp_path, "smoke") == "smoke-005"
    assert next_run_id(tmp_path, "holdout") == "holdout-001"


def test_outcomes_round_trip(tmp_path):
    outcomes = [TaskOutcome(
        task_id="5", level="easy", answer="NL", correct=True,
        key_source="upstream-dev", terminal_status="completed",
        flags=["x"],
    )]
    path = tmp_path / "outcomes.json"
    path.write_text(json.dumps([outcomes[0].__dict__]))
    assert load_outcomes(path) == outcomes
