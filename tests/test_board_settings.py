"""BoardSettings: defaults and env overrides."""
from surogates.config import BoardSettings, get_board_settings


def test_board_settings_defaults():
    s = BoardSettings()
    assert s.snapshot_window_tokens == 600
    assert s.delta_max_chars == 1200
    assert s.read_tool_window_tokens == 1500
    assert s.claim_ttl_seconds == 300
    assert s.max_active_claims_per_writer == 2
    assert s.max_notes_per_group == 300
    assert s.purge_after_days == 7


def test_board_settings_env_override(monkeypatch):
    monkeypatch.setenv("SUROGATES_BOARD_CLAIM_TTL_SECONDS", "600")
    s = BoardSettings()
    assert s.claim_ttl_seconds == 600


def test_get_board_settings_cached():
    get_board_settings.cache_clear()
    assert get_board_settings() is get_board_settings()
