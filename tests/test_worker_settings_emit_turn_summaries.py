"""The two summary switches, which are deliberately separate."""

from __future__ import annotations

from surogates.config import WorkerSettings


def test_iteration_one_liners_are_on_by_default() -> None:
    """They are written while the turn runs, so they cost the tail little
    and are what the Simple chat view renders as the agent works."""
    assert WorkerSettings().emit_turn_summaries is True


def test_end_of_turn_recap_is_off_by_default() -> None:
    """The recap is written after the agent has stopped talking, so it
    sits between the last word and session.complete -- 14.6s at p50 on
    turns that ran one, against a 0.24s baseline."""
    assert WorkerSettings().emit_turn_recap is False


def test_each_switch_moves_independently(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_WORKER_EMIT_TURN_RECAP", "true")
    monkeypatch.setenv("SUROGATES_WORKER_EMIT_TURN_SUMMARIES", "false")
    settings = WorkerSettings()
    assert settings.emit_turn_recap is True
    assert settings.emit_turn_summaries is False
