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


def test_eval_session_is_detected_by_stamped_boundary():
    assert is_eval_session(_session({"memory_boundary": "eval:run-1"})) is True


def test_ordinary_api_session_is_not_an_eval_session():
    assert is_eval_session(_session({})) is False


def test_blank_boundary_is_not_an_eval_session():
    assert is_eval_session(_session({"memory_boundary": "  "})) is False


def test_eval_partition_id_without_stamped_boundary_is_not_an_eval_session():
    # A web client can supply eval_partition_id in config, but without the
    # server-stamped boundary, it's not an isolated session and must keep
    # ask_user_question.
    assert is_eval_session(_session({"eval_partition_id": "run-1-a1b2"})) is False


def test_non_eval_memory_boundary_is_not_an_eval_session():
    # A session with a server-stamped non-eval boundary (e.g., from a Slack channel)
    # is not an evaluation session.
    assert is_eval_session(_session({"memory_boundary": "slack:c:C123"})) is False


def test_eval_session_loses_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"memory_boundary": "eval:run-1"}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" not in result


def test_eval_session_keeps_every_other_tool():
    # The point is evaluating the real agent, so only the tool that cannot
    # possibly be answered is removed.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"memory_boundary": "eval:run-1"}),
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


def test_session_with_eval_partition_id_but_no_boundary_keeps_ask_user_question():
    # Regression test: a client-supplied eval_partition_id without the
    # server-stamped boundary must NOT strip ask_user_question.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_partition_id": "run-1-a1b2"}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" in result
