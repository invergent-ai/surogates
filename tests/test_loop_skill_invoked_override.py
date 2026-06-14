"""Tests for ``skill.invoked`` event enrichment when an override is used."""

from __future__ import annotations

from surogates.harness.loop import _skill_invoked_event_data


def test_event_data_plain():
    data = _skill_invoked_event_data(
        skill_name="browser-research",
        raw_message="/browser-research x",
        staged_at="/ws/.skills/browser-research",
        session_config={},
    )
    assert data == {
        "skill": "browser-research",
        "raw_message": "/browser-research x",
        "staged_at": "/ws/.skills/browser-research",
    }


def test_event_data_with_override():
    cfg = {"skill_overrides": {"browser-research": {
        "content": "...", "source": "skillopt",
        "run_id": "run-1", "candidate_id": "cand-2",
    }}}
    data = _skill_invoked_event_data(
        skill_name="browser-research",
        raw_message="/browser-research x",
        staged_at=None,
        session_config=cfg,
    )
    assert data["override_source"] == "skillopt"
    assert data["skillopt_run_id"] == "run-1"
    assert data["candidate_id"] == "cand-2"


def test_event_data_no_override_for_this_skill():
    cfg = {"skill_overrides": {"other": {"content": "...", "run_id": "r"}}}
    data = _skill_invoked_event_data(
        skill_name="browser-research", raw_message="/x", staged_at=None,
        session_config=cfg,
    )
    assert "override_source" not in data


def test_event_data_none_session_config():
    data = _skill_invoked_event_data(
        skill_name="s", raw_message="/s", staged_at=None, session_config=None,
    )
    assert "override_source" not in data
    assert data["skill"] == "s"
