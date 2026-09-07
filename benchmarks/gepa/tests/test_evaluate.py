"""Feedback must never leak the answer, and infra failures must not score."""
import json

import pytest
from gaia_bench.client import Event
from gaia_bench.dataset import Task
from gaia_bench.runner import RolloutResult

from promptgepa.evaluate import (
    PlatformUnhealthy, health_gate, is_infra_shaped, side_info,
)

GOLD = "Mercedes Sosa"


def rollout(**kw):
    base = dict(
        task_id="abcd1234-0000", session_id="s1", answer="Joan Baez",
        events=[
            Event(id=1, type="tool.call", data={"name": "web_search"}),
            Event(id=2, type="llm.response",
                  data={"message": {"content": "I could not verify this. " + GOLD[:0]}}),
        ],
        wall_clock_s=12.5, terminal_status="completed", error=None,
    )
    base.update(kw)
    return RolloutResult(**base)


def test_side_info_cannot_carry_the_expected_answer():
    info = side_info(rollout(), level=2, role="train", flags=["no_tool_use"],
                     strict=False, lenient=False)
    dumped = json.dumps(info)
    assert GOLD not in dumped
    # The gold answer is not merely absent, it is out of scope: side_info is
    # handed a level and never the Task that holds final_answer.
    assert not {k for k in info if any(
        word in k for word in ("expected", "gold", "reference", "final_answer")
    )}


def test_side_info_carries_what_reflection_needs():
    info = side_info(rollout(), level=3, role="guard",
                     flags=["no_final_answer"], strict=False, lenient=True)
    assert info["solved"] is False
    assert info["right_answer_wrong_format"] is True
    assert info["failure_flags"] == ["no_final_answer"]
    assert info["tool_calls"] == ["web_search"]
    assert info["assistant_turns"] == 1
    assert info["role"] == "guard"


def test_long_output_is_clipped():
    info = side_info(rollout(answer="x" * 5000), level=1, role="train",
                     flags=[], strict=False, lenient=False)
    assert len(info["agent_answer"]) < 400
    assert "+4700 chars" in info["agent_answer"]


@pytest.mark.parametrize("kw,flags,expected", [
    ({}, [], False),
    ({"error": "TransportError: boom"}, [], True),
    ({"terminal_status": "timeout"}, [], True),
    ({"terminal_status": "error"}, [], True),
    ({}, ["empty_llm_response"], True),
    ({}, ["infra_error"], True),
    ({}, ["no_tool_use"], False),
    ({}, ["no_final_answer"], False),
])
def test_infra_shaped_separates_platform_from_prompt(kw, flags, expected):
    assert is_infra_shaped(rollout(**kw), flags) is expected


def test_health_gate_fires_on_a_platform_outage(tmp_path):
    with pytest.raises(PlatformUnhealthy, match="6/24"):
        health_gate(6, 24, where=tmp_path)


def test_health_gate_tolerates_the_baseline_empty_response_rate(tmp_path):
    # ~3 empty_llm_response per 110 tasks is the observed floor; a 24-task
    # batch must not abort on one of them.
    health_gate(1, 24, where=tmp_path)


def test_health_gate_has_a_floor_for_small_batches(tmp_path):
    # 25% of 4 is 1, but one bad rollout in a tiny batch is not an outage.
    health_gate(2, 4, where=tmp_path)
    with pytest.raises(PlatformUnhealthy):
        health_gate(3, 4, where=tmp_path)


def test_a_candidate_that_will_not_load_scores_zero_instead_of_stopping(tmp_path):
    """A degenerate proposal is a bad candidate, not a broken run."""
    from promptgepa.evaluate import make_batch_evaluator
    from promptgepa.harness import CandidateRejected

    class RefusingHarness:
        def render(self, body):
            return body

        def running(self, body):
            raise CandidateRejected("candidate body is empty")

    task = Task(task_id="abcd1234", question="q", level=1,
                final_answer=GOLD, file_name="", file_path="")
    evaluate = make_batch_evaluator(
        harness=RefusingHarness(), tasks_by_id={task.task_id: task},
        roles={task.task_id: "train"},
        out_root=tmp_path, base_url="http://x", token="t", agent_id="a",
    )
    scores = evaluate([("   ", {"task_id": task.task_id, "role": "train"})])

    assert scores[0][0] == 0.0
    assert "did not load" in scores[0][1]["rejected"]
