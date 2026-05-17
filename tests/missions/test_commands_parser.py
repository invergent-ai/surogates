"""Unit tests for /mission slash command parsing."""
from __future__ import annotations

import pytest


def test_parse_create_with_rubric():
    """A full create command extracts description + rubric."""
    from surogates.missions.commands import parse_mission_command

    raw = (
        "Train 0.6B model. Iterate datasets, training, eval.\n\n"
        "Rubric:\n"
        "Reach gsm8k score >= 0.8 (verifier task reports result_metadata.score)"
    )
    cmd = parse_mission_command(raw)
    assert cmd.action == "create"
    assert "Train 0.6B model" in cmd.description
    assert "verifier task" in cmd.rubric
    assert "Rubric:" not in cmd.description


def test_parse_create_rejects_missing_rubric():
    """`/mission <text>` without a Rubric: block fails parse."""
    from surogates.missions.commands import (
        MissionCommandParseError,
        parse_mission_command,
    )

    with pytest.raises(MissionCommandParseError, match="Rubric"):
        parse_mission_command("just a description")


def test_parse_status():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("status")
    assert cmd.action == "status"


def test_parse_pause_with_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("pause waiting on data review")
    assert cmd.action == "pause"
    assert cmd.reason == "waiting on data review"


def test_parse_pause_without_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("pause")
    assert cmd.action == "pause"
    assert cmd.reason is None


def test_parse_resume():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("resume")
    assert cmd.action == "resume"


def test_parse_cancel_with_reason_and_cascade_flag():
    """`cancel --cascade <reason>` sets cascade_to_workers and captures reason."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("cancel --cascade not viable anymore")
    assert cmd.action == "cancel"
    assert cmd.reason == "not viable anymore"
    assert cmd.cascade_to_workers is True


def test_parse_cancel_without_cascade_default_false():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("cancel done with this")
    assert cmd.action == "cancel"
    assert cmd.cascade_to_workers is False
    assert cmd.reason == "done with this"


def test_parse_empty_returns_status():
    """`/mission` with no args is a status query."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("")
    assert cmd.action == "status"


def test_mission_is_reserved_from_slash_skill_expansion():
    """`/mission` must not be treated as a dynamic skill invocation."""
    from surogates.harness.slash_skill import parse_slash_command

    assert parse_slash_command("/mission train model") is None
