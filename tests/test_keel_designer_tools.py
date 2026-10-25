"""The designer agent works on the draft a builder opened it on, through Keel's door.

Each of its ``keel_designer`` tools asks Keel, under the session's id, and
Keel acts on the one draft the builder opened the designer on: it lists and
reads the draft's files and the project's published ones, saves a file or a
scenario, and dry-runs the draft.  None publishes.  A session Keel holds no
draft for is told to have the designer opened, and no case's session reaches
Keel through these tools.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from surogates.keel.client import use_keel_client
from surogates.session.models import Session
from tests.test_case_step_isolation import CONTRACT
from tests.test_case_step_tools import (  # noqa: F401 - fake_keel is a fixture
    _FakeKeel,
    _registry,
    _step,
    fake_keel,
)
from tests.test_keel_case_tools import _case_agent

LOAN = {"kind": "case_type", "key": "loan"}
SCENARIO = {"case_type": "loan", "name": "approved", "steps": []}
FILES = {"draft_id": "d1", "files": [{"path": "case_types/loan.json", "kind": "case_type", "key": "loan"}]}
DRY_RUN = {"head": "c2", "problems": [], "scenarios": ["approved"], "moves": [], "ops_checked": False}
NO_DELEGATION = "No draft is open to this chat: ask the builder to open the designer on a draft."

# Each tool, its arguments, and the request Keel's door gets: the method, the
# route under the session, the query and the body (None: no body).
CALLS = {
    "draft_files": ({}, "GET", "draft/files", {}, None),
    "draft_read": ({"path": "case_types/loan.json"}, "GET", "draft/file", {"path": "case_types/loan.json"}, None),
    "draft_main": ({}, "GET", "draft/main", {}, None),
    "draft_save_file": (
        {"path": "case_types/loan.json", "content": LOAN}, "PUT", "draft/file", {"path": "case_types/loan.json"}, LOAN,
    ),
    "draft_save_scenario": (
        {"path": "tests/loan/approved.json", "content": SCENARIO}, "PUT", "draft/scenario",
        {"path": "tests/loan/approved.json"}, SCENARIO,
    ),
    "draft_dry_run": ({}, "POST", "draft/dry-run", {}, None),
}


def _designer() -> Session:
    """A builder's chat with the designer agent: no case."""
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="designer", channel="studio",
        status="active", config={"channel": "studio"}, created_at=now, updated_at=now,
    )


async def _call(name: str, arguments: dict, session: Session) -> str:
    return await _registry().dispatch(
        name, arguments,
        session_id=str(session.id), session_config=deepcopy(session.config), tool_call_id="c1",
    )


@pytest.mark.parametrize(("tool", "arguments", "method", "route", "params", "body", "answer"), [
    ("draft_files", *CALLS["draft_files"], FILES),
    ("draft_read", *CALLS["draft_read"], {"path": "case_types/loan.json", "content": LOAN}),
    ("draft_main", *CALLS["draft_main"], {"files": ["case_types/loan.json"]}),
    (
        "draft_main", {"path": "case_types/loan.json"}, "GET", "draft/main", {"path": "case_types/loan.json"}, None,
        {"path": "case_types/loan.json", "content": LOAN},
    ),
    ("draft_save_file", *CALLS["draft_save_file"], {"head": "c2"}),
    ("draft_save_scenario", *CALLS["draft_save_scenario"], {"head": "c3"}),
    ("draft_dry_run", *CALLS["draft_dry_run"], DRY_RUN),
], ids=["files", "read", "main-list", "main-file", "save-file", "save-scenario", "dry-run"])
async def test_each_designer_tool_calls_its_door_route(
    fake_keel: _FakeKeel, tool, arguments, method, route, params, body, answer,
):
    designer = _designer()
    fake_keel.answer(200, answer)

    result = await _call(tool, arguments, designer)

    [request] = fake_keel.requests
    assert request.method == method
    assert request.url.path == f"/api/harness/sessions/{designer.id}/{route}"
    assert dict(request.url.params) == params
    assert request.headers["authorization"] == "Bearer door-key"
    if body is None:
        assert request.content == b""
    else:
        assert json.loads(request.content) == body
    assert json.loads(result) == answer


async def test_a_file_keel_finds_invalid_comes_back_with_its_problems(fake_keel: _FakeKeel):
    problems = [{"path": "/stages", "message": "required"}]
    fake_keel.answer(422, {"code": "invalid-file", "message": "The file is not a valid case type.", "problems": problems})

    for tool in ("draft_save_file", "draft_save_scenario"):
        result = json.loads(await _call(tool, CALLS[tool][0], _designer()))

        assert result == {"error": "The file is not a valid case type.", "code": "invalid-file", "problems": problems}


async def test_a_session_without_a_delegation_is_told_to_open_the_designer(fake_keel: _FakeKeel):
    fake_keel.answer(404, {"code": "no-delegation", "message": "This session holds no delegation."})

    for tool, (arguments, *_) in CALLS.items():
        assert json.loads(await _call(tool, arguments, _designer())) == {"error": NO_DELEGATION}, tool
    assert len(fake_keel.requests) == len(CALLS)


async def test_a_file_over_the_doors_limit_is_never_sent(fake_keel: _FakeKeel):
    long = {**LOAN, "description": "x" * 300_000}

    for tool in ("draft_save_file", "draft_save_scenario"):
        result = json.loads(await _call(tool, {"path": "case_types/loan.json", "content": long}, _designer()))

        assert result == {"error": "The file is over the 256,000 bytes Keel takes: split it or shorten it."}
    assert fake_keel.requests == []


async def test_a_call_without_its_path_or_its_file_is_never_sent(fake_keel: _FakeKeel):
    designer = _designer()

    for tool, arguments in (
        ("draft_read", {}),
        ("draft_read", {"path": " "}),
        ("draft_main", {"path": ""}),
        ("draft_save_file", {"content": LOAN}),
        ("draft_save_file", {"path": "case_types/loan.json", "content": "kind: case_type"}),
        ("draft_save_scenario", {"path": "tests/loan/approved.json"}),
    ):
        assert "error" in json.loads(await _call(tool, arguments, designer)), (tool, arguments)
    assert fake_keel.requests == []


async def test_a_file_too_long_to_give_whole_comes_back_cut(fake_keel: _FakeKeel):
    # Written by a builder in the designer, which takes files larger than the door does.
    content = {**LOAN, "description": "\"quoted\"\n" * 50_000}
    fake_keel.answer(200, {"path": "case_types/loan.json", "content": content})

    result = json.loads(await _call("draft_read", {"path": "case_types/loan.json"}, _designer()))

    assert result["path"] == "case_types/loan.json"
    assert result["truncated"] is True
    assert 0 < len(result["content"]) < len(json.dumps(content, ensure_ascii=False))
    assert json.dumps(content, ensure_ascii=False).startswith(result["content"])


async def test_a_designer_tool_in_a_case_session_reaches_nothing(fake_keel: _FakeKeel):
    for session in (_step(deepcopy(CONTRACT)), _case_agent()):
        for tool, (arguments, *_) in CALLS.items():
            result = json.loads(await _call(tool, arguments, session))
            assert "error" in result, (tool, session.config)
    assert fake_keel.requests == []


async def test_without_a_link_to_keel_a_designer_tool_says_so(fake_keel: _FakeKeel):
    use_keel_client(None)

    assert json.loads(await _call("draft_files", {}, _designer())) == {"error": "This runtime has no link to Keel."}
