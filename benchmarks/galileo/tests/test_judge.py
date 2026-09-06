"""AC/TSQ verdict mapping and the missing-verdict rule."""
import pytest

from galbench.judge import (
    JudgeError,
    judge_action_completion,
    judge_tool_selection,
)


async def test_ac_maps_verdicts_and_fails_missing():
    async def complete(messages, schema):
        return {"goals": [
            {"index": 0, "accomplished": True, "evidence": "did it"},
            # index 1 unanswered
        ]}

    verdicts = await judge_action_completion(
        complete, ("goal a", "goal b"), [{"role": "user", "content": "hi"}]
    )
    assert verdicts[0].accomplished is True
    assert verdicts[1].accomplished is False
    assert "no verdict" in verdicts[1].evidence


async def test_ac_rejects_shapeless_reply():
    async def complete(messages, schema):
        return {"nope": 1}

    with pytest.raises(JudgeError, match="no goals list"):
        await judge_action_completion(complete, ("g",), [])


async def test_tsq_empty_calls_short_circuits():
    async def complete(messages, schema):  # pragma: no cover - must not run
        raise AssertionError("should not be called")

    assert await judge_tool_selection(complete, [], "[]", []) == []


async def test_tsq_maps_and_non_bool_is_bad():
    async def complete(messages, schema):
        return {"calls": [
            {"index": 0, "good": "yes", "issue": ""},
            {"index": 1, "good": True, "issue": ""},
        ]}

    calls = [
        {"tool_name": "a", "tool_args": {}},
        {"tool_name": "b", "tool_args": {}},
    ]
    verdicts = await judge_tool_selection(complete, calls, "[]", [])
    assert verdicts[0].good is False
    assert verdicts[1].good is True
