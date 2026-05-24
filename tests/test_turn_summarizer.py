"""TurnSummarizer unit tests with a stubbed summary LLM client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from surogates.harness.turn_summarizer import (
    TurnArtifact,
    TurnSummarizer,
    TurnSummary,
)


@dataclass
class _StubResponse:
    content: str

    @property
    def choices(self):
        return [
            type(
                "Choice", (),
                {"message": type("Msg", (), {"content": self.content})()},
            )()
        ]


class _StubChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, content: str) -> None:
        self.completions = _StubChatCompletions(content)


class _StubClient:
    def __init__(self, content: str) -> None:
        self.chat = _StubChat(content)


@pytest.mark.asyncio
async def test_summarize_iteration_returns_one_liner() -> None:
    client = _StubClient("Rework hero paragraph to introduce brain/hands metaphor")
    summarizer = TurnSummarizer(summary_client=client, summary_model="cheap-model")

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="Let me consider the hero text...",
        tool_calls=[
            {"id": "c1", "function": {"name": "patch",
                                      "arguments": '{"path":"landing.html"}'}},
        ],
        prior_iteration_summaries=[],
    )

    assert result == "Rework hero paragraph to introduce brain/hands metaphor"
    assert client.chat.completions.calls[0]["model"] == "cheap-model"
    # Iteration prompt mentions tool names so the model has context.
    user_block = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "patch" in user_block


@pytest.mark.asyncio
async def test_summarize_iteration_strips_quotes_and_trailing_period() -> None:
    client = _StubClient('"Outline the patch plan."')
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="x",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result == "Outline the patch plan"


@pytest.mark.asyncio
async def test_summarize_iteration_returns_none_on_empty_input() -> None:
    client = _StubClient("noise")
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None
    # Empty input must not waste a model call.
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_summarize_iteration_returns_none_on_empty_response() -> None:
    client = _StubClient("")
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="some reasoning",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_iteration_includes_prior_summaries_in_prompt() -> None:
    client = _StubClient("Apply the rewrite")
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    await summarizer.summarize_iteration(
        iteration_id="i1",
        reasoning="Now applying.",
        tool_calls=[],
        prior_iteration_summaries=["Outline the patch plan"],
    )

    user_block = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "Outline the patch plan" in user_block


@pytest.mark.asyncio
async def test_summarize_iteration_returns_none_on_client_exception() -> None:
    class _Boom:
        chat = type(
            "X", (),
            {"completions": type(
                "Y", (),
                {"create": staticmethod(
                    lambda **_: (_ for _ in ()).throw(RuntimeError("network down"))
                )},
            )()},
        )

    summarizer = TurnSummarizer(summary_client=_Boom(), summary_model="m")
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="x",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_turn_returns_recap_and_artifacts() -> None:
    payload = (
        '{"recap": "Reworked the hero around brain/hands.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": "landing.html", "ref": "landing.html"},'
        '   {"kind": "url", "label": "example.com", "ref": "https://example.com"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="please update the hero",
        iteration_summaries=["Rework hero paragraph"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="landing.html", ref="landing.html"),
            TurnArtifact(kind="url", label="example.com", ref="https://example.com"),
        ],
    )

    assert isinstance(result, TurnSummary)
    assert result.recap.startswith("Reworked the hero")
    assert len(result.artifacts) == 2
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].label == "landing.html"


@pytest.mark.asyncio
async def test_summarize_turn_drops_unknown_artifact_kinds() -> None:
    payload = (
        '{"recap": "Did stuff.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": "good.txt", "ref": "good.txt"},'
        '   {"kind": "weirdo", "label": "bad", "ref": "bad"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="x",
        iteration_summaries=["s"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="good.txt", ref="good.txt"),
        ],
    )

    assert result is not None
    assert len(result.artifacts) == 1
    assert result.artifacts[0].kind == "file"


@pytest.mark.asyncio
async def test_summarize_turn_returns_none_on_invalid_json() -> None:
    client = _StubClient("not JSON at all")
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="hi",
        iteration_summaries=["s"],
        candidate_artifacts=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_turn_returns_none_when_inputs_empty() -> None:
    client = _StubClient("noise")
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="hi",
        iteration_summaries=[],
        candidate_artifacts=[],
    )
    assert result is None
    # Skip the model call entirely when there's nothing to summarize.
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_summarize_turn_returns_none_when_recap_and_artifacts_empty() -> None:
    """LLM returned a structurally-valid response but empty fields."""
    client = _StubClient('{"recap": "", "artifacts": []}')
    summarizer = TurnSummarizer(summary_client=client, summary_model="m")
    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="x",
        iteration_summaries=["s"],
        candidate_artifacts=[],
    )
    assert result is None
