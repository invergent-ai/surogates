"""Unit tests for evidence-gated rubric refinement.

The judge may author a replacement rubric; it may never apply one. These
cover the authoring half -- the schema and the command parsing. The
applying half is DB-backed and lives in
tests/integration/missions/test_refinement.py.
"""
from __future__ import annotations


def test_verdict_defaults_the_proposal_fields_empty():
    """An existing judge response, unaware of refinement, still validates."""
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    v = _MissionVerdict.model_validate(
        {"result": "blocked", "explanation": "stuck", "feedback": ""},
    )
    assert v.proposed_rubric == ""
    assert v.refinement_evidence == ""


def test_verdict_carries_a_proposal():
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    v = _MissionVerdict.model_validate({
        "result": "blocked",
        "explanation": "the rubric names a file the build never emits",
        "feedback": "",
        "proposed_rubric": "Satisfied when dist/bundle.js exists and tests pass.",
        "refinement_evidence": "T9f2a build task: no dist/app.js in output tree",
    })
    assert v.proposed_rubric.startswith("Satisfied when dist/bundle.js")
    assert "T9f2a" in v.refinement_evidence


def test_verdict_dump_always_carries_both_keys():
    """Task 3 reads these off a plain dict; they must never be absent."""
    from surogates.harness.loop_mission_evaluator import _MissionVerdict

    dumped = _MissionVerdict.model_validate({"result": "satisfied"}).model_dump()
    assert "proposed_rubric" in dumped
    assert "refinement_evidence" in dumped


def test_accept_and_reject_parse_as_control_verbs():
    from surogates.missions.commands import parse_mission_command

    assert parse_mission_command("accept").action == "accept"
    assert parse_mission_command("reject").action == "reject"


def test_accept_is_case_insensitive_like_the_other_verbs():
    from surogates.missions.commands import parse_mission_command

    assert parse_mission_command("ACCEPT").action == "accept"


def test_reject_captures_a_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("reject the new rubric drops the test gate")
    assert cmd.action == "reject"
    assert cmd.reason == "the new rubric drops the test gate"


def test_a_description_starting_with_the_word_accept_still_creates():
    """Control verbs are matched on the first token only; a mission whose
    description happens to begin with 'accepted' must not be swallowed."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command(
        "accepted-payments service needs a health check\n\nRubric:\n200 on /health",
    )
    assert cmd.action == "create"
