"""TurnSummarizer unit tests with a stubbed summary LLM client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from surogates.harness.turn_summarizer import (
    TurnArtifact,
    TurnSummarizer,
    TurnSummary,
    _normalize_caption,
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


def _calls(*names: str) -> list[dict[str, Any]]:
    """One tool call per name, ids ``c0``, ``c1``, … to match _results."""
    return [
        {"id": f"c{i}", "function": {"name": n, "arguments": '{"path": "a.py"}'}}
        for i, n in enumerate(names)
    ]


def _results(*contents: str) -> list[dict[str, Any]]:
    return [
        {"tool_call_id": f"c{i}", "content": c}
        for i, c in enumerate(contents)
    ]


_OK = '{"success": true, "name": "x"}'


async def _summarize_with(content: str) -> str | None:
    """Run one patch iteration through the summarizer stubbed to reply."""
    client = _StubClient(content)
    summarizer = _iteration_summarizer(client)
    return await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_calls("patch"),
        prior_iteration_summaries=[],
        tool_results=_results("patched"),
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
        # Typographic apostrophes are the model's default; matching only
        # the ASCII form let every curly-quoted role-play through.
        (
            "role-play with a typographic apostrophe",
            "I\u2019ll load the copywriting skill next",
        ),
        (
            "let-us role-play with a typographic apostrophe",
            "Let\u2019s review the grading protocol",
        ),
        # The transcript format changed in the same commit; a one-line
        # echo of the new shape has no newline to catch it.
        (
            "single-line echo of the reshaped transcript",
            'tool=patch args={"path": "a.py"} returned: patched',
        ),
        (
            "single-line echo of the old transcript",
            'call: skill_view({"name": "x"}) result: {"ok": 1}',
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
        # ``result:`` and ``call:`` occur in real captions; only their
        # co-occurrence marks a transcript echo.
        "Search returns no result: falls back to pypdf",
        "Retry after the failed call: pdftotext is missing",
        "Now searching the workspace for the invoice template",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("skill_view",), ("skill_view", "skill_view")])
async def test_successful_skill_loads_skip_the_model(
    names: tuple[str, ...],
) -> None:
    client = _StubClient("Reviews the copywriting skill")
    summarizer = _iteration_summarizer(client)

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="I should load the skill first",
        tool_calls=_calls(*names),
        prior_iteration_summaries=[],
        tool_results=_results(*([_OK] * len(names))),
    )

    assert result is None
    assert client.chat.completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reasoning", "calls", "results"),
    [
        # Consumers hide these from the condensed view, so with no
        # caption the iteration has nothing left to draw. Only
        # skill_view has a deterministic row to fall back to.
        ("skills_list", "", _calls("skills_list"), _results(_OK)),
        ("list_files", "", _calls("list_files"), _results(_OK)),
        ("search_files", "", _calls("search_files"), _results(_OK)),
        ("session_search", "", _calls("session_search"), _results(_OK)),
        # One call nothing can render on its own is enough: the batch
        # is no longer self-describing.
        ("mixed batch", "", _calls("skill_view", "web_extract"),
         _results(_OK, "Starter 19 EUR")),
        # A failed load is news. Consumers drop errored calls, so
        # skipping the caption would erase the iteration outright.
        ("failed skill load", "", _calls("skill_view"),
         _results('{"success": false, "error": "Skill not found."}')),
        ("skill load with no tenant", "", _calls("skill_view"),
         _results('{"error": "No tenant context available"}')),
        # Success cannot be confirmed with no results in hand, so the
        # caption is produced rather than gambled away.
        ("skill load, results not captured", "", _calls("skill_view"), []),
        # No tool calls at all is not self-describing — there is
        # nothing to draw, so the reasoning still needs a caption.
        ("text-only iteration", "Weighing whether to ship behind a flag",
         [], []),
    ],
)
async def test_iterations_that_still_need_a_caption(
    label: str,
    reasoning: str,
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    client = _StubClient("Loaded the grading rubric")
    summarizer = _iteration_summarizer(client)

    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning=reasoning,
        tool_calls=calls,
        prior_iteration_summaries=[],
        tool_results=results,
    )

    assert result == "Loaded the grading rubric", label
    assert len(client.chat.completions.calls) == 1, label


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
        tool_calls=_calls("patch"),
        prior_iteration_summaries=[],
        tool_results=_results("patched"),
    )

    user_block = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "tool=patch" in user_block
    assert not _valid_iteration_summary(user_block)


# ----------------------------------------------------------------------
# Narrator-prefix normalization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The agent reviewed the rubric", "Reviewed the rubric"),
        ("Agent found AMLR regulation page on EUR-Lex",
         "Found AMLR regulation page on EUR-Lex"),
        ("the agent read Article 5", "Read Article 5"),
        # Nothing to strip — left exactly as written.
        ("Reviewed the rubric", "Reviewed the rubric"),
        ("Agents coordinate through the board", "Agents coordinate through the board"),
        ("", ""),
        # A bare opener with no body must not become an empty caption.
        ("Agent", "Agent"),
    ],
)
def test_normalize_caption(raw: str, expected: str) -> None:
    assert _normalize_caption(raw) == expected


@pytest.mark.asyncio
async def test_summarize_iteration_strips_the_narrator_prefix() -> None:
    assert await _summarize_with(
        "The agent reviewed the copywriting skill guidelines",
    ) == "Reviewed the copywriting skill guidelines"


# ----------------------------------------------------------------------
# JSON-mode captions
#
# The iteration call asks for ``{"caption": str}`` under
# ``response_format``, the same mechanism ``summarize_turn`` already
# uses. A conforming gateway cannot echo the transcript, leak tool-call
# markup, or return a bullet list — the reply has to be an object. A
# gateway that ignores the constraint still returns prose, which is
# used as-is so captions degrade rather than disappear.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iteration_call_asks_for_a_json_object() -> None:
    client = _StubClient('{"caption": "Patched the loader"}')
    summarizer = _iteration_summarizer(client)

    await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_calls("patch"),
        prior_iteration_summaries=[],
        tool_results=_results("patched"),
    )

    kwargs = client.chat.completions.calls[0]
    assert kwargs["response_format"] == {"type": "json_object"}
    # The wrapper costs tokens the caption used to have to itself; a
    # reply truncated mid-object parses to nothing at all.
    assert kwargs["max_tokens"] >= 96


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reply"),
    [
        ("bare object", '{"caption": "Patched the loader"}'),
        (
            "fenced object — gateways that ignore response_format",
            '```json\n{"caption": "Patched the loader"}\n```',
        ),
        (
            "object with surrounding prose",
            'Here you go: {"caption": "Patched the loader"}',
        ),
        ("quoted caption value", '{"caption": "\\"Patched the loader\\""}'),
        ("trailing period", '{"caption": "Patched the loader."}'),
        # A gateway that ignores response_format outright still returns
        # a usable caption; losing those would be worse than the echoes
        # this whole guard exists to stop.
        ("plain prose fallback", "Patched the loader"),
    ],
)
async def test_caption_is_extracted_from_the_reply(
    label: str, reply: str,
) -> None:
    assert await _summarize_with(reply) == "Patched the loader", label


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reply"),
    [
        # The echoed tool result is itself a JSON object, so it parses —
        # but it carries no caption field, and the raw body must not
        # become the label either.
        (
            "echoed tool result",
            '{"success": true, "name": "social", "description": "..."}',
        ),
        ("caption is not a string", '{"caption": {"text": "hi"}}'),
        ("caption is empty", '{"caption": "   "}'),
        # Structure inside the caption field still has to be rejected:
        # response_format constrains the envelope, not the contents.
        (
            "transcript echoed inside the caption",
            '{"caption": "call: patch({}) result: patched"}',
        ),
        (
            "role-play inside the caption",
            '{"caption": "I\\u2019ll patch the loader next"}',
        ),
    ],
)
async def test_malformed_json_replies_are_discarded(
    label: str, reply: str,
) -> None:
    assert await _summarize_with(reply) is None, label


# ----------------------------------------------------------------------
# Reply-channel and embedded-object handling
#
# Mirrors what structured_output's JSON-mode fallback already does, and
# for the same reasons: leaked reasoning can restate the tool arguments
# as an object before the real answer, and reasoning-mode models put the
# object in a different channel than the prose.
# ----------------------------------------------------------------------


class _StubMessage:
    def __init__(self, content: Any, reasoning: Any = None) -> None:
        self.content = content
        self.reasoning_content = reasoning


class _ChannelResponse:
    def __init__(self, content: Any, reasoning: Any = None) -> None:
        self.choices = [type("Choice", (), {"message": _StubMessage(content, reasoning)})()]


class _ChannelCompletions:
    def __init__(self, content: Any, reasoning: Any = None) -> None:
        self._content, self._reasoning = content, reasoning
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _ChannelResponse:
        self.calls.append(kwargs)
        return _ChannelResponse(self._content, self._reasoning)


class _ChannelClient:
    def __init__(self, content: Any, reasoning: Any = None) -> None:
        self.chat = type("Chat", (), {"completions": _ChannelCompletions(content, reasoning)})()


async def _summarize_channels(content: Any, reasoning: Any = None) -> str | None:
    summarizer = _iteration_summarizer(_ChannelClient(content, reasoning))
    return await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_calls("patch"),
        prior_iteration_summaries=[],
        tool_results=_results("patched"),
    )


@pytest.mark.asyncio
async def test_caption_survives_an_object_printed_before_it() -> None:
    # Leaked reasoning restates the tool arguments as an object. Taking
    # only the *first* embedded object loses the caption and drops the
    # whole reply onto the prose path, where a single-line echo passes
    # validation and becomes the visible label.
    assert await _summarize_with(
        'Tool args were {"path": "a.py"} so the caption is'
        ' {"caption": "Patched the loader"}',
    ) == "Patched the loader"


@pytest.mark.asyncio
async def test_caption_is_read_from_the_reasoning_channel() -> None:
    # Reasoning-mode models spend the token budget thinking and leave
    # ``content`` empty with the object in ``reasoning_content``.
    assert await _summarize_channels(
        "", '{"caption": "Patched the loader"}',
    ) == "Patched the loader"


@pytest.mark.asyncio
async def test_content_channel_wins_when_both_are_present() -> None:
    assert await _summarize_channels(
        '{"caption": "From content"}', '{"caption": "From reasoning"}',
    ) == "From content"


@pytest.mark.asyncio
async def test_both_channels_empty_yields_no_caption() -> None:
    assert await _summarize_channels("   ", None) is None


# ----------------------------------------------------------------------
# response_format is a request, not a guarantee
# ----------------------------------------------------------------------


class _RejectsResponseFormat:
    """A provider that 400s on ``response_format`` and works without it."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []
        self.chat = type("Chat", (), {"completions": self})()
        self.completions = self

    async def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError("unsupported parameter: response_format")
        return _StubResponse(self._reply)


@pytest.mark.asyncio
async def test_a_provider_rejecting_json_mode_still_produces_captions() -> None:
    # Silently losing every caption on the deployment is the one outcome
    # worse than an unvalidated one, so the constraint is dropped and
    # the call retried in plain-text mode.
    client = _RejectsResponseFormat("Patched the loader")
    summarizer = _iteration_summarizer(client)

    async def run() -> str | None:
        return await summarizer.summarize_iteration(
            iteration_id="i0",
            reasoning="",
            tool_calls=_calls("patch"),
            prior_iteration_summaries=[],
            tool_results=_results("patched"),
        )

    assert await run() == "Patched the loader"
    assert "response_format" in client.calls[0]
    assert "response_format" not in client.calls[1]

    # The rejection is remembered, so the next iteration does not pay
    # for the failed attempt again.
    assert await run() == "Patched the loader"
    assert len(client.calls) == 3
    assert "response_format" not in client.calls[2]


@pytest.mark.asyncio
async def test_a_timeout_does_not_disable_json_mode() -> None:
    # A slow provider is not a non-conforming one; giving up the
    # constraint on the first timeout would lose it permanently.
    class _Slow:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.chat = type("Chat", (), {"completions": self})()
            self.completions = self

        async def create(self, **kwargs: Any) -> _StubResponse:
            self.calls.append(kwargs)
            raise asyncio.TimeoutError

    client = _Slow()
    summarizer = _iteration_summarizer(client)
    result = await summarizer.summarize_iteration(
        iteration_id="i0",
        reasoning="",
        tool_calls=_calls("patch"),
        prior_iteration_summaries=[],
        tool_results=_results("patched"),
    )

    assert result is None
    assert len(client.calls) == 1
    assert "response_format" in client.calls[0]
