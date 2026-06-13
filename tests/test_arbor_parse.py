"""Unit tests for /auto-research command parsing."""
from __future__ import annotations

import pytest

from surogates.missions.commands import (
    MissionCommandParseError,
    parse_auto_research_command,
)


def test_parse_create_with_leading_kv_tokens():
    cmd = parse_auto_research_command(
        "repo=/workspace/repo max_iterations=60 baseline=0.41 baseline_test=0.50 "
        "Improve F1\n\nRubric:\n- test_trunk_score improves"
    )
    assert cmd.action == "create"
    assert cmd.max_iterations == 60
    assert cmd.repo == "/workspace/repo"
    assert cmd.baseline == 0.41 and cmd.baseline_test == 0.50
    assert cmd.description.startswith("Improve F1")
    assert "test_trunk_score" in cmd.rubric


def test_parse_rejects_non_numeric_baseline():
    with pytest.raises(MissionCommandParseError):
        parse_auto_research_command("baseline=abc x\n\nRubric:\nr")


def test_parse_rejects_non_integer_max_iterations():
    with pytest.raises(MissionCommandParseError):
        parse_auto_research_command("max_iterations=lots x\n\nRubric:\nr")


def test_parse_resume_token():
    cmd = parse_auto_research_command(
        "resume=2b1d34aa max_iterations=40 continue\n\nRubric:\nr"
    )
    assert cmd.resume_run == "2b1d34aa" and cmd.max_iterations == 40


def test_parse_control_verbs_delegate():
    assert parse_auto_research_command("pause taking a break").action == "pause"
    assert parse_auto_research_command("").action == "status"
    assert parse_auto_research_command("cancel --cascade done").action == "cancel"


def test_parse_requires_rubric():
    with pytest.raises(MissionCommandParseError):
        parse_auto_research_command("max_iterations=10 no rubric here")


def test_parse_error_is_auto_research_specific():
    # The error must guide toward /auto-research + repo=, not leak /mission.
    with pytest.raises(MissionCommandParseError) as ei:
        parse_auto_research_command("improve accuracy of the classifier")
    msg = str(ei.value)
    assert "/auto-research" in msg and "repo=" in msg
    assert "/mission" not in msg


def test_parse_plain_create_without_tokens():
    cmd = parse_auto_research_command("Optimize the model\n\nRubric:\n- improves")
    assert cmd.action == "create"
    assert cmd.repo is None and cmd.max_iterations is None
    assert cmd.description == "Optimize the model"
