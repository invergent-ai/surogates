"""TurnSummarizer unit tests with a stubbed summary LLM client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from surogates.harness.turn_summarizer import (
    TurnArtifact,
    TurnSummarizer,
    TurnSummary,
    _valid_iteration_summary,
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


def _iteration_summarizer(client: Any, model: str = "m") -> TurnSummarizer:
    """Summarizer whose cheap summary slot is the stub under test."""
    return TurnSummarizer(
        base_client=_StubClient("unused"),
        base_model="base-model",
        summary_client=client,
        summary_model=model,
    )


def _turn_summarizer(client: Any, model: str = "m") -> TurnSummarizer:
    """Summarizer whose base slot is the stub under test."""
    return TurnSummarizer(base_client=client, base_model=model)


@pytest.mark.asyncio
async def test_summarize_iteration_returns_one_liner() -> None:
    client = _StubClient("Rework hero paragraph to introduce brain/hands metaphor")
    summarizer = _iteration_summarizer(client, "cheap-model")

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
    summarizer = _iteration_summarizer(client)
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
    summarizer = _iteration_summarizer(client)

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
    summarizer = _iteration_summarizer(client)

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
    summarizer = _iteration_summarizer(client)

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

    summarizer = _iteration_summarizer(_Boom())
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="x",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_turn_returns_recap_and_downloadable_artifacts() -> None:
    # The model echoing a url-kind entry must not survive parsing —
    # the summary card only presents downloadable artifacts.
    payload = (
        '{"recap": "Reworked the hero around brain/hands.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": "landing.html", "ref": "landing.html"},'
        '   {"kind": "url", "label": "example.com", "ref": "https://example.com"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client, "base-model")

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="please update the hero",
        iteration_summaries=["Rework hero paragraph"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="landing.html", ref="landing.html"),
        ],
    )

    assert isinstance(result, TurnSummary)
    assert result.recap.startswith("Reworked the hero")
    assert len(result.artifacts) == 1
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].label == "landing.html"
    # The turn summary runs on the base model, not the cheap one.
    assert client.chat.completions.calls[0]["model"] == "base-model"


@pytest.mark.asyncio
async def test_summarize_turn_drops_web_urls_smuggled_as_files() -> None:
    payload = (
        '{"recap": "Fetched the paper.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": "paper", "ref": "https://example.com/p.pdf"},'
        '   {"kind": "file", "label": "report.pdf", "ref": "report.pdf"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client)

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="x",
        iteration_summaries=["s"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="report.pdf", ref="report.pdf"),
        ],
    )

    assert result is not None
    assert [a.ref for a in result.artifacts] == ["report.pdf"]


@pytest.mark.asyncio
async def test_summarize_iteration_skipped_without_summary_model() -> None:
    """No cheap summary model configured: iteration summaries are
    skipped gracefully (turn summaries still run on the base model)."""
    base = _StubClient("unused")
    summarizer = TurnSummarizer(base_client=base, base_model="base-model")

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="some reasoning",
        tool_calls=[],
        prior_iteration_summaries=[],
    )
    assert result is None
    assert base.chat.completions.calls == []


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
    summarizer = _turn_summarizer(client)

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
    summarizer = _turn_summarizer(client)

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
    summarizer = _turn_summarizer(client)

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
    summarizer = _turn_summarizer(client)
    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="x",
        iteration_summaries=["s"],
        candidate_artifacts=[],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_turn_drops_internal_workspace_paths() -> None:
    """Even if the model echoes an internal path (e.g. the
    /product-marketing skill's .agents/ context file), it must not
    reach the user-visible download card."""
    payload = (
        '{"recap": "Built the marketing context.",'
        ' "artifacts": ['
        '   {"kind": "file", "label": ".agents/product-marketing.md",'
        '    "ref": ".agents/product-marketing.md"}'
        ' ]}'
    )
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client)

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="create a marketing document",
        iteration_summaries=["Write product marketing context"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="x", ref="x"),
        ],
    )

    # recap survives; the internal file does not.
    assert result is not None
    assert result.artifacts == []


@pytest.mark.asyncio
async def test_summarize_turn_parses_markdown_fenced_json() -> None:
    # The exact failure shape that silenced recaps in production:
    # Claude via an OpenAI-compatible gateway ignores
    # response_format=json_object and wraps the object in a ```json
    # fence. The recap must still land.
    payload = (
        "```json\n"
        '{\n  "recap": "Quizzed the user on 5 Greek verbs and updated '
        'the progress tracker.",\n  "artifacts": []\n}\n'
        "```"
    )
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client)

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="help me practice greek verbs",
        iteration_summaries=["Quiz user on verbs"],
        candidate_artifacts=[],
    )

    assert isinstance(result, TurnSummary)
    assert result.recap.startswith("Quizzed the user")
    assert result.artifacts == []


@pytest.mark.asyncio
async def test_summarize_turn_parses_json_with_surrounding_prose() -> None:
    payload = (
        "Here is the summary you asked for:\n"
        "```json\n"
        '{"recap": "Built the report.", "artifacts": '
        '[{"kind": "file", "label": "report.pdf", "ref": "report.pdf"}]}\n'
        "```\n"
        "Let me know if you need anything else."
    )
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client)

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="make me a report",
        iteration_summaries=["Write report"],
        candidate_artifacts=[
            TurnArtifact(kind="file", label="report.pdf", ref="report.pdf"),
        ],
    )

    assert isinstance(result, TurnSummary)
    assert result.recap == "Built the report."
    assert [a.ref for a in result.artifacts] == ["report.pdf"]


@pytest.mark.asyncio
async def test_summarize_turn_returns_none_on_truncated_fenced_json() -> None:
    # A max_tokens cutoff mid-object stays unparseable — no recap
    # beats a silently wrong one.
    payload = '```json\n{"recap": "The agent loaded the PostHog analytics'
    client = _StubClient(payload)
    summarizer = _turn_summarizer(client)

    result = await summarizer.summarize_turn(
        turn_id="t1",
        user_message="stats please",
        iteration_summaries=["Run queries"],
        candidate_artifacts=[],
    )

    assert result is None


# ----------------------------------------------------------------------
# Malformed-reply rejection
#
# The cheap summary model periodically completes the prompt's transcript
# instead of captioning it. Whatever it returns becomes the iteration's
# user-visible label, so every structural failure shape observed in
# production is rejected here; the caller then emits no event and the
# chat client falls back to its deterministic tool-derived label.
# ----------------------------------------------------------------------


def _patch_call() -> list[dict[str, Any]]:
    return [
        {
            "id": "c1",
            "function": {"name": "patch", "arguments": '{"path": "a.py"}'},
        },
    ]


async def _summarize_with(content: str) -> str | None:
    client = _StubClient(content)
    summarizer = _iteration_summarizer(client)
    return await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_patch_call(),
        prior_iteration_summaries=[],
        tool_results=[{"tool_call_id": "c1", "content": "patched"}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reply"),
    [
        (
            "verbatim transcript echo",
            'call: skill_view({"name": "copywriting"})\n'
            '  result: {"success": true, "name": "copywriting"}',
        ),
        (
            "reshaped transcript echo",
            '[1] tool=patch args={"path": "a.py"}\n    returned: patched',
        ),
        (
            "prompt header echo",
            "Tools called (with result snippets):\ncall: patch({})",
        ),
        (
            "raw json result",
            '{"success": true, "name": "social", "description": "..."}',
        ),
        (
            "json array",
            '[{"path": "a.py"}]',
        ),
        (
            "deepseek tool-call markup",
            "I'll load the skill.\n\n<｜DSML｜tool_calls>",
        ),
        (
            "xml tool-call markup",
            '<tool_call>{"name": "patch"}</tool_call>',
        ),
        (
            "fenced code block",
            "```json\n{}\n```",
        ),
        (
            "markdown bullet list",
            "- Searched Camillo Borghese\n- Searched Alfonso Visconti",
        ),
        (
            "single leading bullet",
            "- Searched for the Borghese page",
        ),
        (
            "first-person role-play",
            "I'll start by creating the directory and making the calls",
        ),
        (
            "let-me role-play",
            "Let me check what tools are available instead",
        ),
        (
            "based-on role-play",
            "Based on the brief, I will now review the grading protocol",
        ),
        (
            "multi-line prose dump",
            "Setup repo and clone it\n\nList files in working tree",
        ),
        (
            "transcript dumped past the word cap",
            " ".join(f"word{i}" for i in range(40)),
        ),
    ],
)
async def test_summarize_iteration_rejects_malformed_reply(
    label: str, reply: str,
) -> None:
    assert await _summarize_with(reply) is None, label


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "Patch the loader to skip empty rows",
        "Find pdftotext fails — falling back to pypdf",
        "Reviews copywriting skill guidelines for tone and structure",
        # An em-dash clause and a colon are ordinary caption prose and
        # must not trip the structural markers.
        "Cloned the repo: 3 submodules initialised",
    ],
)
async def test_summarize_iteration_keeps_well_formed_reply(reply: str) -> None:
    assert await _summarize_with(reply) == reply


@pytest.mark.asyncio
async def test_summarize_iteration_strips_then_validates() -> None:
    # Quote-stripping runs first, so a quoted echo is still rejected.
    assert await _summarize_with('"call: patch({})"') is None


# ----------------------------------------------------------------------
# Self-describing iterations are not summarized at all
# ----------------------------------------------------------------------


def _calls(*names: str) -> list[dict[str, Any]]:
    return [
        {"id": f"c{i}", "function": {"name": n, "arguments": "{}"}}
        for i, n in enumerate(names)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names",
    [
        ("skill_view",),
        ("skills_list",),
        ("list_files",),
        ("search_files",),
        ("skill_view", "skill_view"),
        ("skill_view", "search_files"),
    ],
)
async def test_self_describing_iterations_skip_the_model(
    names: tuple[str, ...],
) -> None:
    client = _StubClient("Reviews the copywriting skill")
    summarizer = _iteration_summarizer(client)

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="I should load the skill first",
        tool_calls=_calls(*names),
        prior_iteration_summaries=[],
    )

    assert result is None
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_mixed_batch_still_summarizes() -> None:
    # One non-self-describing call is enough: the client cannot label
    # the batch on its own, so the summary still earns its model call.
    client = _StubClient("Fetch the pricing page after loading the skill")
    summarizer = _iteration_summarizer(client)

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_calls("skill_view", "web_extract"),
        prior_iteration_summaries=[],
    )

    assert result == "Fetch the pricing page after loading the skill"
    assert len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_text_only_iteration_still_summarizes() -> None:
    # No tool calls at all is not "self-describing" — there is nothing
    # for the client to render, so the reasoning still needs a caption.
    client = _StubClient("Weigh two rollout options")
    summarizer = _iteration_summarizer(client)

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="Considering whether to ship behind a flag",
        tool_calls=[],
        prior_iteration_summaries=[],
    )

    assert result == "Weigh two rollout options"


@pytest.mark.asyncio
async def test_transcript_lines_are_not_caption_shaped() -> None:
    # The transcript the model sees must not look like a valid answer,
    # or it completes the list instead of summarizing it. Guarded by
    # feeding each rendered line back through the reply validator.
    client = _StubClient("Patch the loader")
    summarizer = _iteration_summarizer(client)

    await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_patch_call(),
        prior_iteration_summaries=[],
        tool_results=[{"tool_call_id": "c1", "content": "patched"}],
    )

    user_block = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "tool=patch" in user_block
    assert not _valid_iteration_summary(user_block)
