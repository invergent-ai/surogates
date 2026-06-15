"""Schema tests for the prompt-submission ``SkillOverride`` support.

Covers the new ``SkillOverride`` model and the optional
``skill_overrides`` field on :class:`PromptRequest` that the ops
SkillOpt worker uses to attach a candidate ``SKILL.md`` body to a single
service-account session.
"""

from __future__ import annotations

from surogates.api.routes.prompts import PromptRequest, SkillOverride


def test_skill_override_defaults():
    ov = SkillOverride(content="# candidate body")
    assert ov.content == "# candidate body"
    assert ov.type == "skill"
    assert ov.source == "skillopt"
    assert ov.description is None
    assert ov.run_id is None


def test_prompt_request_accepts_skill_overrides():
    req = PromptRequest(
        prompt="/browser-research compare vendors",
        skill_overrides={
            "browser-research": {
                "content": "# improved body",
                "description": "Research and summarize web information.",
                "trigger": "research, web lookup",
                "run_id": "run-1",
                "candidate_id": "cand-2",
            }
        },
    )
    assert req.skill_overrides is not None
    ov = req.skill_overrides["browser-research"]
    assert ov.content == "# improved body"
    assert ov.run_id == "run-1"


def test_prompt_request_skill_overrides_optional():
    req = PromptRequest(prompt="hello")
    assert req.skill_overrides is None
