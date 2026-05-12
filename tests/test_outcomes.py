from __future__ import annotations

from surogates.harness.outcomes import (
    DEFAULT_OUTCOME_RUBRIC,
    OutcomeCommand,
    OutcomeState,
    build_continuation_prompt,
    parse_goal_command,
    start_outcome,
)


def test_parse_goal_status_defaults_for_empty_args() -> None:
    assert parse_goal_command("") == OutcomeCommand(action="status", text="", rubric="")
    assert parse_goal_command("status") == OutcomeCommand(action="status", text="", rubric="")


def test_parse_goal_controls() -> None:
    assert parse_goal_command("pause").action == "pause"
    assert parse_goal_command("resume").action == "resume"
    assert parse_goal_command("clear").action == "clear"


def test_parse_goal_description_with_rubric_heading() -> None:
    command = parse_goal_command(
        "Build a DCF model\n\nRubric:\n"
        "- Creates an xlsx file\n"
        "- Includes sensitivity analysis"
    )

    assert command.action == "set"
    assert command.text == "Build a DCF model"
    assert "Creates an xlsx file" in command.rubric


def test_start_outcome_uses_default_rubric_when_missing() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )

    assert state.description == "Fix tests"
    assert state.rubric == DEFAULT_OUTCOME_RUBRIC
    assert state.status == "active"
    assert state.max_iterations == 3
    assert state.iteration == 0
    assert state.id.startswith("outc_")


def test_start_outcome_clamps_max_iterations_to_twenty() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=99,
        now_iso="2026-05-12T10:00:00Z",
    )

    assert state.max_iterations == 20


def test_outcome_state_round_trips_from_config() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="Must pass pytest",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )

    loaded = OutcomeState.from_config(state.to_config())

    assert loaded == state


def test_build_continuation_prompt_includes_feedback_and_rubric() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="Must pass pytest",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )
    state.last_feedback = "pytest still fails in tests/test_api.py"

    prompt = build_continuation_prompt(state)

    assert "[Continuing toward your defined outcome]" in prompt
    assert "Fix tests" in prompt
    assert "Must pass pytest" in prompt
    assert "pytest still fails" in prompt
