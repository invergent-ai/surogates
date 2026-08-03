"""A judge cannot declare a mission satisfied with nothing to show for it.

`adjust_research_verdict` already implements "the LLM does not mint the
terminal verdict" for research missions: `satisfied` is demoted unless a
machine-written score improved and the report task is done. Standard missions
had no such gate — the judge's word was final.

The corroborating signal here is deliberately machine-written: task status is
set by the dispatcher and the completion classifier, never by the coordinator's
own prose. A `tool.result` event would NOT do — it proves some tool ran, and
the coordinator chooses the tool and its arguments.

Nothing here executes anything. Declared validator commands are a separate,
larger design (sandbox access, timeouts, argv trust) and are not this.
"""
from __future__ import annotations

import pytest

from surogates.missions.verdict_policy import adjust_mission_verdict


def _v(result: str) -> dict:
    return {"result": result, "explanation": "e", "feedback": "f"}


def test_satisfied_with_completed_work_stands():
    out = adjust_mission_verdict(_v("satisfied"), completed_tasks=2)
    assert out["result"] == "satisfied"


def test_satisfied_with_nothing_done_is_demoted():
    out = adjust_mission_verdict(
        _v("satisfied"), completed_tasks=0, total_tasks=2,
    )
    assert out["result"] == "needs_revision"
    assert "corroborat" in out["explanation"].lower()


def test_the_demotion_explains_itself_to_the_agent():
    out = adjust_mission_verdict(
        _v("satisfied"), completed_tasks=0, total_tasks=2,
    )
    assert out["feedback"] != "f", "must replace the judge's own feedback"


@pytest.mark.parametrize("result", ["needs_revision", "blocked", "failed"])
def test_non_terminal_verdicts_pass_through(result):
    """Only `satisfied` claims completion; the rest are already conservative."""
    out = adjust_mission_verdict(_v(result), completed_tasks=0, total_tasks=3)
    assert out["result"] == result


def test_a_taskless_mission_can_still_be_satisfied():
    """Not every mission decomposes into tasks. When none were ever created
    there is nothing to corroborate against and the gate must not deadlock."""
    out = adjust_mission_verdict(
        _v("satisfied"), completed_tasks=0, total_tasks=0,
    )
    assert out["result"] == "satisfied"


def test_a_mission_with_tasks_none_done_is_demoted():
    out = adjust_mission_verdict(
        _v("satisfied"), completed_tasks=0, total_tasks=3,
    )
    assert out["result"] == "needs_revision"


def test_a_malformed_verdict_is_left_alone():
    assert adjust_mission_verdict({}, completed_tasks=0) == {}
