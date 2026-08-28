import pytest

from gaia_bench.attribute import (
    ATTRIBUTION_SCHEMA,
    ROOT_CAUSES,
    attribute,
    format_trajectory,
)
from gaia_bench.client import Event


def test_every_root_cause_has_an_owner():
    assert ROOT_CAUSES["search_no_results"] == "web_search"
    assert ROOT_CAUSES["page_content_missed"] == "browser"
    assert ROOT_CAUSES["file_parse_failed"] == "file_ops"
    assert ROOT_CAUSES["wrong_retrieval_path"] == "tool_selection"
    assert ROOT_CAUSES["ambiguous_ground_truth"] == "benchmark"
    assert all(isinstance(v, str) and v for v in ROOT_CAUSES.values())


def test_schema_enumerates_exactly_the_known_root_causes():
    enum = ATTRIBUTION_SCHEMA["schema"]["properties"]["root_cause"]["enum"]
    assert set(enum) == set(ROOT_CAUSES)


def test_schema_requires_evidence():
    required = ATTRIBUTION_SCHEMA["schema"]["required"]
    assert "root_cause" in required
    assert "evidence" in required


def test_format_trajectory_renders_calls_and_results():
    events = [
        Event(id=1, type="user.message", data={"content": "q"}),
        Event(id=2, type="tool.call",
              data={"name": "web_search", "arguments": {"query": "x"}}),
        Event(id=3, type="tool.result",
              data={"name": "web_search", "content": "no results"}),
        Event(id=4, type="llm.response",
              data={"message": {"content": "FINAL ANSWER: 4"}}),
    ]
    text = format_trajectory(events)
    assert "web_search" in text
    assert "no results" in text
    assert "FINAL ANSWER: 4" in text


def test_format_trajectory_drops_delta_noise():
    events = [
        Event(id=i, type="llm.delta", data={"text": "tok"}) for i in range(50)
    ] + [Event(id=99, type="llm.response",
               data={"message": {"content": "done"}})]
    text = format_trajectory(events)
    assert "llm.delta" not in text
    assert "done" in text


def test_format_trajectory_truncates_to_budget():
    events = [
        Event(id=i, type="tool.result",
              data={"name": "read_file", "content": "x" * 5000})
        for i in range(50)
    ]
    text = format_trajectory(events, max_chars=10000)
    assert len(text) <= 10000 + 200  # allow the truncation notice


async def test_attribute_returns_root_cause_and_owner():
    async def fake_complete(messages, schema):
        return {"root_cause": "search_no_results",
                "evidence": "web_search returned zero results at event 3",
                "hypothesis": "query too narrow"}

    out = await attribute(
        fake_complete, question="q", ground_truth="4",
        model_answer="5", events=[],
    )
    assert out["root_cause"] == "search_no_results"
    assert out["owner"] == "web_search"
    assert out["evidence"]


async def test_attribute_rejects_unknown_root_cause():
    async def fake_complete(messages, schema):
        return {"root_cause": "gremlins", "evidence": "e"}

    with pytest.raises(ValueError, match="gremlins"):
        await attribute(fake_complete, question="q", ground_truth="4",
                        model_answer="5", events=[])


async def test_prompt_constrains_output_without_relying_on_response_format():
    """Not every provider honours response_format.

    yunwu returns 200 OK and free-form markdown, ignoring the json_schema
    entirely, so the prompt itself must pin both the output format and the
    allowed labels or every verdict is unusable.
    """
    seen = {}

    async def fake_complete(messages, schema):
        seen["system"] = messages[0]["content"]
        return {"root_cause": "reasoning_error", "evidence": "e"}

    await attribute(fake_complete, question="q", ground_truth="4",
                    model_answer="5", events=[])
    system = seen["system"]
    # Every allowed label must be named, or the model invents its own.
    for cause in ROOT_CAUSES:
        assert cause in system, f"{cause} missing from the prompt"
    assert "JSON" in system.upper()


async def test_attribute_passes_the_schema_through():
    seen = {}

    async def fake_complete(messages, schema):
        seen["schema"] = schema
        seen["messages"] = messages
        return {"root_cause": "reasoning_error", "evidence": "e"}

    await attribute(fake_complete, question="the question",
                    ground_truth="4", model_answer="5", events=[])
    assert seen["schema"] is ATTRIBUTION_SCHEMA
    assert "the question" in seen["messages"][-1]["content"]
