"""Tests for the ``skill_overrides_enabled`` worker kill switch."""

from __future__ import annotations

from surogates.config import WorkerSettings


def test_skill_overrides_enabled_defaults_true():
    s = WorkerSettings()
    assert s.skill_overrides_enabled is True


def test_skill_overrides_enabled_env_override(monkeypatch):
    monkeypatch.setenv("SUROGATES_WORKER_SKILL_OVERRIDES_ENABLED", "false")
    s = WorkerSettings()
    assert s.skill_overrides_enabled is False
