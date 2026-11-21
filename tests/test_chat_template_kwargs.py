"""Chat-template arguments the deployment sets for its model, sent with every main-loop request and the advisor's.

A model whose chat template reads its reasoning effort as a number (Inkling: ``chat_template_kwargs.reasoning_effort``
0.99) could not be set from the harness: the session's ``reasoning_effort`` goes out as the OpenAI field, which takes a
word. ``llm.chat_template_kwargs`` (SUROGATES_LLM_CHAT_TEMPLATE_KWARGS) now sets them for the deployment, a session's
own ``chat_template_kwargs`` wins key by key, a sub-agent on the same model inherits them, and the built-in advisor is
asked the same way where it runs on the same model (``llm.advisor_same_model``). config.yaml gives the setting as a
mapping, which reaches it whole. Unset, nothing changes.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import surogates.harness.llm_call as llm_call
from surogates.config import LLMSettings, _flatten_yaml
from surogates.harness.llm_call import merged_chat_template_kwargs, sampling_kwargs
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin.expert_loop import _generation_kwargs


@pytest.fixture
def deployment(monkeypatch):
    """Set the deployment's chat-template arguments for one test."""
    def set_(value: dict):
        monkeypatch.setattr(llm_call, "deployment_chat_template_kwargs", lambda: dict(value))
    return set_


def test_unset_sends_nothing_as_before(deployment):
    deployment({})
    assert sampling_kwargs({}) == {}
    assert sampling_kwargs({"enable_thinking": False}) == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_the_deployments_arguments_go_with_every_request(deployment):
    deployment({"reasoning_effort": 0.99})
    assert sampling_kwargs({}) == {"extra_body": {"chat_template_kwargs": {"reasoning_effort": 0.99}}}
    assert sampling_kwargs({"temperature": 1.0, "enable_thinking": True}) == {
        "temperature": 1.0,
        "extra_body": {"chat_template_kwargs": {"reasoning_effort": 0.99, "enable_thinking": True}},
    }


def test_a_sessions_own_arguments_win_key_by_key(deployment):
    deployment({"reasoning_effort": 0.99, "other": 1})
    got = sampling_kwargs({"chat_template_kwargs": {"reasoning_effort": 0.5}})
    assert got["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": 0.5, "other": 1}
    # A malformed session value is ignored, not a reason to drop the deployment's.
    assert merged_chat_template_kwargs({"chat_template_kwargs": "0.5"}) == {"reasoning_effort": 0.99, "other": 1}


def test_a_session_alone_can_set_them(deployment):
    deployment({})
    assert sampling_kwargs({"chat_template_kwargs": {"reasoning_effort": 0.99}}) == {
        "extra_body": {"chat_template_kwargs": {"reasoning_effort": 0.99}},
    }


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUROGATES_LLM_CHAT_TEMPLATE_KWARGS", '{"reasoning_effort": 0.99}')
    assert LLMSettings().chat_template_kwargs == {"reasoning_effort": 0.99}
    monkeypatch.delenv("SUROGATES_LLM_CHAT_TEMPLATE_KWARGS")
    assert LLMSettings().chat_template_kwargs == {}


def test_the_setting_is_read_from_config_yaml(monkeypatch):
    flat = _flatten_yaml({"llm": {"chat_template_kwargs": {"reasoning_effort": 0.99}, "base_url": "http://e/v1"},
                          "db": {"url": "postgresql://x"}})
    assert flat == {"SUROGATES_LLM_CHAT_TEMPLATE_KWARGS": '{"reasoning_effort": 0.99}',
                    "SUROGATES_LLM_BASE_URL": "http://e/v1", "SUROGATES_DB_URL": "postgresql://x"}
    monkeypatch.setenv("SUROGATES_LLM_CHAT_TEMPLATE_KWARGS", flat["SUROGATES_LLM_CHAT_TEMPLATE_KWARGS"])
    assert LLMSettings().chat_template_kwargs == {"reasoning_effort": 0.99}
    # A value YAML reads as a date cannot break the settings: it goes in as its text.
    import datetime

    dated = _flatten_yaml({"llm": {"chat_template_kwargs": {"when": datetime.date(2026, 2, 12)}}})
    assert dated == {"SUROGATES_LLM_CHAT_TEMPLATE_KWARGS": '{"when": "2026-02-12"}'}


class _Store:
    async def create_session(self, **kwargs):
        return SimpleNamespace(**kwargs)


async def test_a_child_on_the_same_model_inherits_them(deployment):
    deployment({})
    parent = SimpleNamespace(
        id=uuid4(), org_id=uuid4(), user_id=uuid4(), agent_id="a-1", model="inkling", service_account_id=None,
        channel="api", config={"storage_bucket": "b", "storage_key_prefix": "p", "workspace_path": "/workspace",
                               "chat_template_kwargs": {"reasoning_effort": 0.99}},
    )
    child = await create_child_session(store=_Store(), parent=parent, channel="delegation", config={"max_iterations": 30})
    assert child.config["chat_template_kwargs"] == {"reasoning_effort": 0.99}


def test_an_expert_can_carry_them_in_its_generation_settings():
    standard, extra = _generation_kwargs({"temperature": 0.2, "chat_template_kwargs": {"reasoning_effort": 0.99}})
    assert standard == {"temperature": 0.2}
    assert extra == {"chat_template_kwargs": {"reasoning_effort": 0.99}}


async def _run_expert(monkeypatch, expert):
    from unittest.mock import MagicMock

    from surogates.tools.builtin import expert_loop as el

    captured: dict = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            msg = SimpleNamespace(content="done", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _FakeClient:
        def __init__(self, *, base_url, api_key):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

        async def close(self):
            pass

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)
    monkeypatch.setattr("surogates.config.load_settings", lambda: SimpleNamespace(platform_api_url="http://srv:8888"))
    await el.run_expert_loop(expert=expert, task="t", context=None, tool_router=MagicMock(), tool_registry=MagicMock(),
                             tenant=SimpleNamespace(org_config={}), session_id=uuid4())
    return captured


def _advisor():
    from surogates.tools.loader import SkillDef

    return SkillDef(name="advisor", description="c", content="b", source="platform", builtin=True, type="expert",
                    expert_status="active", expert_model="m", expert_endpoint="http://e:8000/v1")


async def test_the_built_in_advisor_on_the_same_model_is_asked_as_the_deployment_asks_it(monkeypatch, deployment):
    import surogates.tools.builtin.advisor_expert as ae

    deployment({"reasoning_effort": 0.99})
    monkeypatch.setattr(ae, "_same_model", lambda: True)
    got = await _run_expert(monkeypatch, _advisor())
    assert got["extra_body"] == {"chat_template_kwargs": {"reasoning_effort": 0.99}}


async def test_the_built_in_advisor_on_another_tier_is_not_given_them(monkeypatch, deployment):
    import surogates.tools.builtin.advisor_expert as ae

    deployment({"reasoning_effort": 0.99})
    monkeypatch.setattr(ae, "_same_model", lambda: False)
    got = await _run_expert(monkeypatch, _advisor())
    assert "extra_body" not in got


async def test_a_tenants_expert_is_not_given_the_deployments_arguments(monkeypatch, deployment):
    from surogates.tools.loader import SkillDef

    deployment({"reasoning_effort": 0.99})
    other = SkillDef(name="drafter", description="c", content="b", source="org", type="expert", expert_status="active",
                     expert_model="m", expert_endpoint="http://e:8000/v1")
    got = await _run_expert(monkeypatch, other)
    assert "extra_body" not in got
