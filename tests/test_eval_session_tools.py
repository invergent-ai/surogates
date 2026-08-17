"""An evaluation session never sees a tool that waits on a human."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.orchestrator.worker import (
    _filter_effective_tools,
    is_eval_session,
)

_TOOLS = {"ask_user_question", "memory", "web_search", "skill_manage"}


def _tenant():
    return SimpleNamespace(user_id=None, service_account_id="sa-1")


def _session(config):
    return SimpleNamespace(channel="api", config=config)


def test_eval_session_is_detected_by_run_id():
    assert is_eval_session(_session({"eval_run_id": "run-1"})) is True


def test_ordinary_api_session_is_not_an_eval_session():
    assert is_eval_session(_session({})) is False


def test_blank_run_id_is_not_an_eval_session():
    assert is_eval_session(_session({"eval_run_id": "  "})) is False


def test_eval_session_loses_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_run_id": "run-1"}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" not in result


def test_eval_session_keeps_every_other_tool():
    # The point is evaluating the real agent, so only the tool that cannot
    # possibly be answered is removed.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_run_id": "run-1"}),
        use_api_for_harness_tools=True,
    )
    assert {"memory", "web_search", "skill_manage"} <= result


def test_ordinary_api_session_keeps_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" in result
