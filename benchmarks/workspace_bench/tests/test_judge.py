"""Judge prompt assembly and verdict parsing."""
import pytest

from wsbench.client import Event
from wsbench.dataset import Task
from wsbench.judge import (
    JudgeError,
    _extract_json,
    build_judge_prompt,
    format_trajectory,
    judge_task,
)


def _task(**overrides):
    fields = dict(
        task_id="3",
        persona="Backend Developer",
        instruction="Extract the dependencies into deps.md.",
        difficulty="medium",
        output_files=("deps.md",),
        rubrics=("Was deps.md created?", "Does it list 43 deps?"),
        rubric_types=("Basic Evaluation", "Outcome Evaluation"),
        tested_capabilities=(),
        manifest=(),
        local_dir="/snap/task_lite_clean_en/3",
    )
    fields.update(overrides)
    return Task(**fields)


def test_extract_json_plain_fenced_and_prose():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('verdict follows {"a": 1} thanks') == {"a": 1}
    with pytest.raises(JudgeError):
        _extract_json("no json here")


def test_build_judge_prompt_contains_everything():
    prompt = build_judge_prompt(
        _task(),
        files=[{"workspace_path": "outputs/deps.md", "text": "43 deps", "note": None}],
        trajectory="[1] CALL read_file {}",
        final_message="done",
    )
    assert "0. [Basic Evaluation] Was deps.md created?" in prompt
    assert "1. [Outcome Evaluation] Does it list 43 deps?" in prompt
    assert "=== FILE: outputs/deps.md ===" in prompt
    assert "43 deps" in prompt
    assert "[1] CALL read_file" in prompt
    assert "done" in prompt


def test_build_judge_prompt_no_files():
    prompt = build_judge_prompt(_task(), files=[], trajectory="", final_message="")
    assert "produced no files" in prompt


def test_format_trajectory_drops_delta_noise():
    events = [
        Event(1, "llm.delta", {"text": "chunk"}),
        Event(2, "tool.call", {"name": "write_file", "arguments": {"p": "x"}}),
        Event(3, "llm.response", {"message": {"content": "final"}}),
    ]
    out = format_trajectory(events)
    assert "chunk" not in out
    assert "CALL write_file" in out
    assert "ASSISTANT final" in out


async def test_judge_task_maps_verdicts_and_fails_missing_indices():
    async def complete(messages, schema):
        return {"rubrics": [
            {"index": 0, "passed": True, "confidence": 0.9, "evidence": "seen"},
            # index 1 never answered
        ]}

    verdicts = await judge_task(complete, _task(), [], [], "")
    assert verdicts[0].passed is True
    assert verdicts[0].rubric_type == "Basic Evaluation"
    assert verdicts[1].passed is False
    assert "no verdict" in verdicts[1].evidence


async def test_judge_task_rejects_shapeless_reply():
    async def complete(messages, schema):
        return {"something": "else"}

    with pytest.raises(JudgeError, match="no rubrics list"):
        await judge_task(complete, _task(), [], [], "")


async def test_judge_task_non_bool_passed_is_failed():
    async def complete(messages, schema):
        return {"rubrics": [
            {"index": 0, "passed": "yes", "evidence": "?"},
            {"index": 1, "passed": True, "evidence": "ok"},
        ]}

    verdicts = await judge_task(complete, _task(), [], [], "")
    assert verdicts[0].passed is False
    assert verdicts[1].passed is True
