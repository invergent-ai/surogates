"""Run-id sequencing per prefix."""
from galbench.cli import next_run_id


def test_next_run_id_starts_at_001(tmp_path):
    assert next_run_id(tmp_path, "dev") == "dev-001"


def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "dev-002").mkdir()
    (tmp_path / "smoke-001").mkdir()
    (tmp_path / "dev-custom").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    assert next_run_id(tmp_path, "dev") == "dev-003"
    assert next_run_id(tmp_path, "smoke") == "smoke-002"
