"""Three advisor fixes from Inkling-2-Mini's 11-21 sessions (AutomationBench, EnterpriseOps-Gym, Claw-Eval).

1. A consult that names no expert went nowhere ("expert name is required": 18 % of Inkling's consults, W4 3 of
   1,149): it now goes to the only expert there is; with several, the error lists them.
2. The advisor read the last 12 messages, 16k characters: the task itself was in that window for 0 % of AutomationBench
   consults. The session's first user message now leads the transcript when it is not among them, the 16k bound held.
3. ``llm.advisor_same_model``: where the advisor runs on the executor's own model it is introduced as a second opinion
   in a fresh context and is not invited to stop (Inkling advising itself told itself to halt in ~45 % of consulted
   sessions). Default: the text as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import surogates.tools.builtin.advisor_expert as ae
import surogates.tools.builtin.expert as ex
from surogates.tools.builtin.advisor_expert import build_advisor_expert, build_expert_transcript

async def _consult(monkeypatch, args, available):
    asked: list[str] = []

    async def resolve(name, loaded_skills, kwargs):
        asked.append(name)
        return None, list(available)

    monkeypatch.setattr(ex, "_resolve_expert_local", resolve)
    out = await ex._consult_expert_handler(args, tenant=MagicMock(), session_id="00000000-0000-0000-0000-000000000001",
                                           tool_router=MagicMock(), tool_registry=MagicMock())
    return json.loads(out), asked


async def test_no_name_goes_to_the_only_expert(monkeypatch):
    data, asked = await _consult(monkeypatch, {"task": "check my plan"}, ["advisor"])
    assert asked[-1] == "advisor"            # resolved under the only name there is
    assert "expert name is required" not in data.get("error", "")


async def test_no_name_with_several_experts_lists_them(monkeypatch):
    data, asked = await _consult(monkeypatch, {"task": "check"}, ["advisor", "sql_writer"])
    assert data["error"] == "expert name is required"
    assert data["available_experts"] == ["advisor", "sql_writer"]


async def test_a_named_expert_is_resolved_as_before(monkeypatch):
    data, asked = await _consult(monkeypatch, {"task": "check", "expert": "sql_writer"}, ["advisor", "sql_writer"])
    assert asked == ["sql_writer"]


class _Api:
    """The bundle-aware API: the advisor resolves by name, the catalog lists the active experts."""

    def __init__(self, experts):
        self.experts = experts
        self.asked: list[str] = []

    async def get_skill(self, name):
        self.asked.append(name)
        if name in self.experts:
            return {"name": name, "type": "expert", "expert_status": "active", "builtin": name == "advisor",
                    "content": "advise"}
        return None

    async def list_skills(self):
        return json.dumps({"skills": [{"name": n, "type": "expert", "expert_status": "active"} for n in self.experts]})


async def _consult_api(args, experts):
    api = _Api(experts)
    out = await ex._consult_expert_handler(args, tenant=MagicMock(), session_id="00000000-0000-0000-0000-000000000001",
                                           tool_router=MagicMock(), tool_registry=MagicMock(), api_client=api)
    return json.loads(out), api.asked


async def test_no_name_goes_to_the_only_expert_through_the_api():
    data, asked = await _consult_api({"task": "check my plan"}, ["advisor"])
    assert asked == ["advisor"]
    # resolved to the advisor, which then needs the session's LLM client (none in this test)
    assert data == {"error": "No LLM client available for the advisor."}


async def test_no_name_with_several_experts_through_the_api_lists_them():
    data, asked = await _consult_api({"task": "check"}, ["advisor", "sql_writer"])
    assert asked == []
    assert data == {"error": "expert name is required", "available_experts": ["advisor", "sql_writer"]}


def _msgs(n: int, task: str = "TASK: reconcile the March invoices") -> list[dict]:
    return [{"role": "user", "content": task}] + [
        {"role": "assistant" if i % 2 else "tool", "content": f"step {i}"} for i in range(n)
    ]


def test_the_task_leads_a_transcript_that_has_lost_it():
    text = build_expert_transcript(_msgs(30))
    assert text.startswith("user (the task, the session's first message): TASK: reconcile the March invoices")
    assert "step 29" in text and "step 5" not in text


def test_the_task_is_not_repeated_when_it_is_in_the_window():
    text = build_expert_transcript(_msgs(3))
    assert text.count("TASK: reconcile") == 1
    assert not text.startswith("user (the task")


def test_the_bound_holds_with_the_task():
    msgs = [{"role": "user", "content": "T" * 10_000}] + [{"role": "assistant", "content": "x" * 3_000} for _ in range(20)]
    text = build_expert_transcript(msgs)
    assert len(text) <= 16_000
    assert text.startswith("user (the task") and "T" * 4_000 in text and "T" * 4_001 not in text


def test_the_advisor_text_is_unchanged_by_default(monkeypatch):
    monkeypatch.setattr(ae, "_same_model", lambda: False)
    skill = build_advisor_expert()
    assert "The executor model is cheaper" in skill.content and "A stop signal is a valid answer." in skill.content
    assert skill.description.startswith("A stronger model")


def test_on_the_same_model_it_is_a_second_opinion_that_does_not_invite_stopping(monkeypatch):
    monkeypatch.setattr(ae, "_same_model", lambda: True)
    skill = build_advisor_expert()
    assert "second opinion" in skill.content and "cheaper" not in skill.content
    assert "A stop signal is a valid answer." not in skill.content
    assert "Recommend stopping only when the task cannot be completed" in " ".join(skill.content.split())
    assert "stronger" not in skill.description


def test_the_setting_is_read_from_the_environment(monkeypatch):
    from surogates.config import LLMSettings

    assert LLMSettings().advisor_same_model is False
    monkeypatch.setenv("SUROGATES_LLM_ADVISOR_SAME_MODEL", "true")
    assert LLMSettings().advisor_same_model is True
