"""The eval memory boundary is server-derived, never client-supplied."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

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


def test_client_supplied_boundary_is_stripped_on_a_non_api_channel():
    # The strip must happen before the channel check, or a web caller could
    # name any conversation's memory partition as its own.
    assert "memory_boundary" not in apply_eval_isolation(
        {"memory_boundary": "eval:run-1"}, channel="web",
    )


def test_a_uuid_run_id_is_accepted():
    run_id = str(uuid4())
    config = apply_eval_isolation({"eval_run_id": run_id}, channel="api")
    assert config["memory_boundary"] == f"eval:{run_id}"


@pytest.mark.parametrize("run_id", [
    "../../../other-org/shared",
    "run 1",
    "run/1",
    "eval:run-1",
    "x" * 65,
])
def test_a_malformed_run_id_is_refused(run_id):
    # The id lands in memory object keys and in on-disk path components, so a
    # separator or a traversal segment would escape the agent's own storage.
    # Refuse rather than drop the boundary: dropping it silently would run the
    # evaluation against the agent's real memory.
    with pytest.raises(HTTPException) as exc:
        apply_eval_isolation({"eval_run_id": run_id}, channel="api")
    assert exc.value.status_code == 422


def test_blank_eval_run_id_is_not_a_boundary():
    config = apply_eval_isolation({"eval_run_id": "   "}, channel="api")
    assert "memory_boundary" not in config


def test_ordinary_config_is_untouched():
    config = apply_eval_isolation({"single_session": True}, channel="api")
    assert config == {"single_session": True}
