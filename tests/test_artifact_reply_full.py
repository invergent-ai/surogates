"""A session read through its reply text gets the deliverable in the reply (session config ``artifact_reply: "full"``).

create_artifact's text ends "After it renders, reply with one line at most", and the artifact guidance sends a report
the user asks for to the artifact panel. An API caller or an evaluation judge reads the reply only: on Claw-Eval
(11-21) Inkling-2-Mini put its report in an artifact in 51 of 90 sessions and passed 47 % of those against 85 % of the
others, the judge naming the missing content. With the setting the prompt says the reply carries the deliverable; an
artifact may be added, never instead. Default unchanged.
"""

from __future__ import annotations

from surogates.harness.prompt import PromptBuilder
from tests.test_case_tool_gate import _chat, _tenant

TOOLS = {"create_artifact", "read_file"}
FULL = "**Your reply is what is read.**"
ARTIFACT = "**Artifacts.**"


def _prompt(**config) -> str:
    session = _chat(dict(config))
    return PromptBuilder(_tenant(session), session=session, available_tools=set(TOOLS)).build()


def test_the_default_prompt_is_unchanged():
    prompt = _prompt()
    assert ARTIFACT in prompt and FULL not in prompt


def test_full_puts_the_deliverable_in_the_reply():
    prompt = _prompt(artifact_reply="full")
    assert ARTIFACT in prompt and FULL in prompt
    assert prompt.index(FULL) > prompt.index(ARTIFACT)


def test_other_values_change_nothing():
    assert FULL not in _prompt(artifact_reply="brief")


def test_without_the_artifact_tool_nothing_is_said():
    session = _chat({"artifact_reply": "full"})
    prompt = PromptBuilder(_tenant(session), session=session, available_tools={"read_file"}).build()
    assert FULL not in prompt and ARTIFACT not in prompt
