"""The eval memory boundary is server-derived, never client-supplied."""
from __future__ import annotations

from surogates.api.routes.sessions import apply_eval_isolation


def test_eval_run_id_produces_a_namespaced_boundary():
    config = apply_eval_isolation({"eval_run_id": "run-1"}, channel="api")
    assert config["memory_boundary"] == "eval:run-1"


def test_client_supplied_boundary_is_stripped():
    config = apply_eval_isolation(
        {"memory_boundary": "slack:c:C123"}, channel="api",
    )
    assert "memory_boundary" not in config


def test_client_boundary_cannot_survive_alongside_an_eval_run_id():
    config = apply_eval_isolation(
        {"eval_run_id": "run-1", "memory_boundary": "slack:c:C123"},
        channel="api",
    )
    assert config["memory_boundary"] == "eval:run-1"


def test_non_api_channel_gets_no_boundary():
    config = apply_eval_isolation({"eval_run_id": "run-1"}, channel="web")
    assert "memory_boundary" not in config


def test_blank_eval_run_id_is_not_a_boundary():
    config = apply_eval_isolation({"eval_run_id": "   "}, channel="api")
    assert "memory_boundary" not in config


def test_ordinary_config_is_untouched():
    config = apply_eval_isolation({"single_session": True}, channel="api")
    assert config == {"single_session": True}
