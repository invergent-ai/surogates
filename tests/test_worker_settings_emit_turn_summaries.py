"""Coverage for the WorkerSettings.emit_turn_summaries switch."""

from __future__ import annotations

from surogates.config import WorkerSettings


def test_worker_settings_default_emit_turn_summaries_is_false() -> None:
    """LLM recaps are off by default.

    The recap sat between the agent's last word and session.complete and
    cost 14.6s at p50 on turns that ran one. The agent now says what it
    produced in its own closing message, which costs nothing.
    """
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is False


def test_worker_settings_emit_turn_summaries_enabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_WORKER_EMIT_TURN_SUMMARIES", "true")
    settings = WorkerSettings()
    assert settings.emit_turn_summaries is True
