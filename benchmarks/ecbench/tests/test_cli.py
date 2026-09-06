"""Run-id sequencing per prefix."""
from ecbench.cli import next_run_id


def test_next_run_id_starts_at_001(tmp_path):
    assert next_run_id(tmp_path, "year") == "year-001"
    assert next_run_id(tmp_path, "smoke") == "smoke-001"


def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "year-001").mkdir()
    (tmp_path / "smoke-001").mkdir()
    (tmp_path / "smoke-007").mkdir()
    assert next_run_id(tmp_path, "year") == "year-002"
    assert next_run_id(tmp_path, "smoke") == "smoke-008"


def test_next_run_id_ignores_stray_entries(tmp_path):
    (tmp_path / "year-002").mkdir()
    (tmp_path / "year-custom").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    assert next_run_id(tmp_path, "year") == "year-003"
