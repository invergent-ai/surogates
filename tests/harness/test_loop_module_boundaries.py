"""Structural checks for the harness loop compatibility facade."""

from __future__ import annotations


def test_loop_facade_reexports_extracted_message_helpers() -> None:
    from surogates.harness import loop
    from surogates.harness.loop_messages import (
        _initial_system_message,
        _view_context_note,
        _view_context_note_from_metadata,
        maybe_inject_browser_pause,
    )

    assert loop.maybe_inject_browser_pause is maybe_inject_browser_pause
    assert loop._initial_system_message is _initial_system_message
    assert loop._view_context_note is _view_context_note
    assert loop._view_context_note_from_metadata is _view_context_note_from_metadata


def test_loop_facade_reexports_extracted_tool_recovery_helper() -> None:
    from surogates.harness import loop
    from surogates.harness.loop_tool_recovery import (
        build_partial_tool_call_recovery_results,
    )

    assert (
        loop.build_partial_tool_call_recovery_results
        is build_partial_tool_call_recovery_results
    )


def test_loop_facade_reexports_extracted_mission_helpers() -> None:
    from surogates.harness import loop
    from surogates.harness.loop_mission_evaluator import (
        MissionJudgeParseError,
        _MissionVerdict,
        _build_mission_judge,
        _maybe_run_mission_evaluator,
        _parse_judge_json,
    )

    assert loop.MissionJudgeParseError is MissionJudgeParseError
    assert loop._MissionVerdict is _MissionVerdict
    assert callable(_build_mission_judge)
    assert callable(loop._build_mission_judge)
    assert loop._maybe_run_mission_evaluator is _maybe_run_mission_evaluator
    assert loop._parse_judge_json is _parse_judge_json
