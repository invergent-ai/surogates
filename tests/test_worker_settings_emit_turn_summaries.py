"""Coverage for the WorkerSettings.emit_turn_summaries kill switch."""

from __future__ import annotations

from surogates.config import WorkerSettings


def test_worker_settings_default_emit_turn_summaries_is_true() -> None:
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is True


def test_worker_settings_emit_turn_summaries_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_WORKER_EMIT_TURN_SUMMARIES", "false")
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is False
