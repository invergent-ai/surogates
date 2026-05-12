from __future__ import annotations

import json

from surogates.harness.outcomes import (
    DEFAULT_OUTCOME_RUBRIC,
    OutcomeCommand,
    OutcomeState,
    apply_evaluation,
    build_continuation_prompt,
    build_evaluator_messages,
    parse_goal_command,
    parse_outcome_evaluation,
    start_outcome,
)
from surogates.config import Settings
from surogates.session.events import EventType


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


def test_parse_outcome_evaluation_accepts_json() -> None:
    evaluation = parse_outcome_evaluation(
        json.dumps({
            "result": "needs_revision",
            "explanation": "One criterion missing",
            "feedback": "Add the sensitivity table",
        })
    )

    assert evaluation.result == "needs_revision"
    assert evaluation.explanation == "One criterion missing"
    assert evaluation.feedback == "Add the sensitivity table"
    assert evaluation.parse_failed is False


def test_parse_outcome_evaluation_extracts_fenced_json() -> None:
    evaluation = parse_outcome_evaluation(
        '```json\n{"result":"satisfied","explanation":"All criteria met","feedback":""}\n```'
    )

    assert evaluation.result == "satisfied"
    assert evaluation.parse_failed is False


def test_parse_outcome_evaluation_marks_bad_output_as_parse_failure() -> None:
    evaluation = parse_outcome_evaluation("I think it is done")

    assert evaluation.result == "needs_revision"
    assert evaluation.parse_failed is True


def test_build_evaluator_messages_contains_outcome_rubric_and_response() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="pytest passes",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )

    messages = build_evaluator_messages(state, "I fixed one file")
    joined = "\n".join(m["content"] for m in messages)

    assert "Fix tests" in joined
    assert "pytest passes" in joined
    assert "I fixed one file" in joined
    assert '"result"' in joined


def test_apply_evaluation_satisfied_marks_state_satisfied() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )

    decision = apply_evaluation(
        state,
        parse_outcome_evaluation(
            '{"result":"satisfied","explanation":"All good","feedback":""}'
        ),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )

    assert state.status == "satisfied"
    assert decision.should_continue is False
    assert decision.result == "satisfied"


def test_apply_evaluation_needs_revision_continues_before_budget() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )

    decision = apply_evaluation(
        state,
        parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Missing test","feedback":"Run pytest"}'
        ),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )

    assert state.status == "active"
    assert state.iteration == 1
    assert state.last_feedback == "Run pytest"
    assert decision.should_continue is True


def test_apply_evaluation_pauses_after_parse_failures() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=3,
        now_iso="2026-05-12T10:00:00Z",
    )
    state.consecutive_parse_failures = 2

    decision = apply_evaluation(
        state,
        parse_outcome_evaluation("not json"),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )

    assert state.status == "paused"
    assert "parse" in (state.paused_reason or "")
    assert decision.should_continue is False


def test_apply_evaluation_stops_at_iteration_budget() -> None:
    state = start_outcome(
        "Fix tests",
        rubric="",
        max_iterations=1,
        now_iso="2026-05-12T10:00:00Z",
    )

    decision = apply_evaluation(
        state,
        parse_outcome_evaluation(
            '{"result":"needs_revision","explanation":"Missing","feedback":"Keep going"}'
        ),
        now_iso="2026-05-12T10:01:00Z",
        max_parse_failures=3,
    )

    assert state.status == "max_iterations_reached"
    assert decision.result == "max_iterations_reached"
    assert decision.should_continue is False


def test_outcome_event_type_values_are_stable() -> None:
    assert EventType.USER_DEFINE_OUTCOME.value == "user.define_outcome"
    assert EventType.OUTCOME_DEFINED.value == "outcome.defined"
    assert (
        EventType.OUTCOME_EVALUATION_START.value
        == "span.outcome_evaluation_start"
    )
    assert (
        EventType.OUTCOME_EVALUATION_ONGOING.value
        == "span.outcome_evaluation_ongoing"
    )
    assert EventType.OUTCOME_EVALUATION_END.value == "span.outcome_evaluation_end"
    assert EventType.OUTCOME_CONTINUATION.value == "outcome.continuation"


def test_outcome_settings_defaults() -> None:
    settings = Settings()

    assert settings.outcomes.max_iterations == 3
    assert settings.outcomes.max_parse_failures == 3
    assert settings.outcomes.evaluator_model == ""
