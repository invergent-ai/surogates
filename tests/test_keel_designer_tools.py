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
import re
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from surogates.keel.client import use_keel_client
from surogates.tools.router import ToolLocation, ToolRouter
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


PROCESS = "processes/review.bpmn"
XML = "<bpmn:definitions/>"
CHECK = {"path": PROCESS, "head": "c2", "changed": True, "problems": [], "ops_checked": False}
REFERENCE = {"rules": [], "elements": [], "refused": [], "keel": [], "problems": [], "layout": [], "examples": []}
NO_PROCESS_ROUTES = (
    "This Keel cannot work on process files yet: tell the builder you can work on its case types, and on its "
    "processes once Keel is upgraded."
)
PROCESS_TOOLS = (
    "draft_process_read", "draft_process_save", "draft_process_edit", "draft_process_check",
    "draft_process_reference",
)
# The three tools that only read, as CALLS gives each: the arguments, the method, the route, the query, the body.
PROCESS_READS = {
    "draft_process_read": ({"path": PROCESS}, "GET", "draft/process", {"path": PROCESS}, None),
    "draft_process_check": ({"path": PROCESS}, "POST", "draft/process/check", {"path": PROCESS}, None),
    "draft_process_reference": ({}, "GET", "draft/process/reference", {}, None),
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
    long = {**LOAN, "description": "x" * 1_048_576}

    for tool in ("draft_save_file", "draft_save_scenario"):
        result = json.loads(await _call(tool, {"path": "case_types/loan.json", "content": long}, _designer()))

        assert result == {"error": "The file is over the 1 MiB Keel takes: split it or shorten it."}
    assert fake_keel.requests == []


async def test_a_file_within_the_doors_limit_is_sent_whole(fake_keel: _FakeKeel):
    fake_keel.answer(200, {"head": "c3"})
    # Over the 256,000 bytes Keel's other routes are sent at most, and within the 1 MiB a draft file takes.
    long = {**LOAN, "description": "x" * 1_000_000}

    for tool in ("draft_save_file", "draft_save_scenario"):
        result = json.loads(await _call(tool, {"path": "case_types/loan.json", "content": long}, _designer()))

        assert result == {"head": "c3"}, tool
    assert [json.loads(request.content) for request in fake_keel.requests] == [long, long]


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


@pytest.mark.parametrize(("tool", "arguments", "method", "route", "params", "body", "answer"), [
    ("draft_process_read", *PROCESS_READS["draft_process_read"], {"path": PROCESS, "xml": XML}),
    (
        "draft_process_read", {"path": PROCESS, "published": True}, "GET", "draft/main/process", {"path": PROCESS},
        None, {"path": PROCESS, "xml": XML},
    ),
    (
        "draft_process_read", {"path": PROCESS, "published": False}, "GET", "draft/process", {"path": PROCESS}, None,
        {"path": PROCESS, "xml": XML},
    ),
    ("draft_process_check", *PROCESS_READS["draft_process_check"], CHECK),
    ("draft_process_reference", *PROCESS_READS["draft_process_reference"], REFERENCE),
], ids=["read", "read-published", "read-draft", "check", "reference"])
async def test_each_process_read_goes_to_its_door_route(
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
    assert request.content == b""
    assert json.loads(result) == answer


async def test_a_process_file_too_long_to_give_whole_comes_back_cut(fake_keel: _FakeKeel):
    xml = "a" * 400_000
    fake_keel.answer(200, {"path": PROCESS, "xml": xml})

    result = json.loads(await _call("draft_process_read", {"path": PROCESS}, _designer()))

    assert result["path"] == PROCESS
    assert result["truncated"] is True
    assert 0 < len(result["xml"]) < len(xml)
    assert xml.startswith(result["xml"])
    assert len(json.dumps(result, ensure_ascii=False)) <= 300_000


async def test_a_process_file_within_the_result_is_given_whole_and_not_marked_cut(fake_keel: _FakeKeel):
    fake_keel.answer(200, {"path": PROCESS, "xml": XML})

    result = json.loads(await _call("draft_process_read", {"path": PROCESS}, _designer()))

    assert result == {"path": PROCESS, "xml": XML}


async def test_a_path_that_names_no_process_file_is_never_sent(fake_keel: _FakeKeel):
    designer = _designer()
    named = "path must name a process's file, processes/<key>.bpmn, as draft_files lists it."

    for tool in ("draft_process_read", "draft_process_check"):
        for arguments in (
            {}, {"path": ""}, {"path": 5}, {"path": "case-types/loan.json"}, {"path": "processes/"},
            {"path": "processes/a/b.bpmn"}, {"path": "processes/review.json"}, {"path": "/processes/review.bpmn"},
        ):
            assert json.loads(await _call(tool, arguments, designer)) == {"error": named}, (tool, arguments)
    for published in ("yes", 1, None):
        result = json.loads(await _call("draft_process_read", {"path": PROCESS, "published": published}, designer))
        assert result == {"error": "published must be true or false, or be left out."}, published
    assert fake_keel.requests == []


async def test_a_keel_without_the_process_routes_is_named_not_taken_for_a_missing_file(fake_keel: _FakeKeel):
    for tool, (arguments, *_) in PROCESS_READS.items():
        fake_keel.answer(404, {"code": "not-found", "message": "Not Found"})

        assert json.loads(await _call(tool, arguments, _designer())) == {"error": NO_PROCESS_ROUTES}, tool



async def test_a_process_file_the_draft_lacks_comes_back_in_keels_words(fake_keel: _FakeKeel):
    fake_keel.answer(404, {"code": "unknown-file", "message": "draft d1 has no processes/review.bpmn"})

    assert json.loads(await _call("draft_process_read", {"path": PROCESS}, _designer())) == {
        "error": "draft d1 has no processes/review.bpmn", "code": "unknown-file",
    }


async def test_an_old_keel_is_not_named_for_a_json_tool(fake_keel: _FakeKeel):
    fake_keel.answer(404, {"code": "not-found", "message": "Not Found"})

    assert json.loads(await _call("draft_files", {}, _designer())) == {"error": "Not Found", "code": "not-found"}


async def test_a_session_without_a_delegation_is_told_to_open_the_designer_on_a_process_tool(fake_keel: _FakeKeel):
    fake_keel.answer(404, {"code": "no-delegation", "message": "This session holds no delegation."})

    for tool, (arguments, *_) in PROCESS_READS.items():
        assert json.loads(await _call(tool, arguments, _designer())) == {"error": NO_DELEGATION}, tool


async def test_a_process_tool_in_a_case_session_reaches_nothing(fake_keel: _FakeKeel):
    for session in (_step(deepcopy(CONTRACT)), _case_agent()):
        for tool, (arguments, *_) in PROCESS_READS.items():
            assert "error" in json.loads(await _call(tool, arguments, session)), (tool, session.config)
    assert fake_keel.requests == []


async def test_without_a_link_to_keel_a_process_tool_says_so(fake_keel: _FakeKeel):
    use_keel_client(None)

    for tool, (arguments, *_) in PROCESS_READS.items():
        assert json.loads(await _call(tool, arguments, _designer())) == {"error": "This runtime has no link to Keel."}


@pytest.mark.parametrize("tool", PROCESS_TOOLS)
def test_the_process_tools_run_in_the_harness(tool: str):
    router = ToolRouter(registry=_registry(), sandbox_pool=None, governance=None)

    assert router.resolve_location(tool) is ToolLocation.HARNESS


def test_a_process_tools_first_sentence_has_no_em_dash_and_says_what_it_does():
    described = {
        schema["function"]["name"]: schema["function"]["description"]
        for schema in _registry().get_schemas({"draft_process_read", "draft_process_check", "draft_process_reference"})
    }
    firsts = {name: re.split(r"(?<=\.)\s", text, maxsplit=1)[0] for name, text in described.items()}

    assert set(described) == set(PROCESS_READS)
    assert all("\u2014" not in text for text in described.values())
    assert firsts["draft_process_read"].startswith("Read one of the draft's process files")
    assert firsts["draft_process_check"].startswith("Check one of the draft's process files as a publish would")
    assert firsts["draft_process_reference"].startswith("Read Keel's reference for process files")


SAVED = {
    "head": "c4", "changed": True,
    "layout": {"problem": None, "laid_out": 5, "kept": 0, "carried": 0, "dropped": 0, "unsupported": [], "grown": []},
}
EDITS = [{"old": "a", "new": "b"}]
NOT_THE_DRAFTS = (
    "draft_process_save and draft_process_edit change only the draft's file: what is published changes when the "
    "builder publishes."
)
JSON_TOOL_ON_A_PROCESS = (
    "processes/<key>.bpmn is a process's BPMN file: read it with draft_process_read, and change it with "
    "draft_process_edit or draft_process_save."
)


@pytest.mark.parametrize(("tool", "arguments", "method", "route", "body"), [
    ("draft_process_save", {"path": PROCESS, "xml": "<x/>"}, "PUT", "draft/process", {"xml": "<x/>"}),
    ("draft_process_edit", {"path": PROCESS, "edits": EDITS}, "POST", "draft/process/edit", {"edits": EDITS}),
], ids=["save", "edit"])
async def test_a_save_and_an_edit_go_to_their_door_routes(fake_keel: _FakeKeel, tool, arguments, method, route, body):
    designer = _designer()
    fake_keel.answer(200, SAVED)

    result = await _call(tool, arguments, designer)

    [request] = fake_keel.requests
    assert request.method == method
    assert request.url.path == f"/api/harness/sessions/{designer.id}/{route}"
    assert dict(request.url.params) == {"path": PROCESS}
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(request.content) == body
    assert json.loads(result) == SAVED


async def test_an_edit_is_sent_with_only_its_old_and_its_new_text(fake_keel: _FakeKeel):
    fake_keel.answer(200, SAVED)

    await _call("draft_process_edit", {"path": PROCESS, "edits": [{"old": "a", "new": "", "x": 1}]}, _designer())

    assert json.loads(fake_keel.requests[0].content) == {"edits": [{"old": "a", "new": ""}]}


async def test_a_save_or_an_edit_naming_published_is_refused_and_nothing_is_sent(fake_keel: _FakeKeel):
    designer = _designer()

    for published in (True, False, "yes"):
        for tool, arguments in (
            ("draft_process_save", {"path": PROCESS, "xml": "<x/>"}),
            ("draft_process_edit", {"path": PROCESS, "edits": EDITS}),
        ):
            result = await _call(tool, {**arguments, "published": published}, designer)
            assert json.loads(result) == {"error": NOT_THE_DRAFTS}, (tool, published)
    assert fake_keel.requests == []


async def test_a_save_without_its_file_or_an_edit_without_its_edits_is_never_sent(fake_keel: _FakeKeel):
    designer = _designer()
    xml = "xml must be the process's whole BPMN XML, as text."
    edits = "edits must be a list of 1 to 50 objects, each with the text to replace (old) and its new text (new)."
    edit = "edit {n}: old must be text of the file, copied exactly, and new the text to put in its place."
    wrong_path = "path must name a process's file, processes/<key>.bpmn, as draft_files lists it."

    for tool, arguments, error in (
        ("draft_process_save", {"path": PROCESS}, xml),
        ("draft_process_save", {"path": PROCESS, "xml": ""}, xml),
        ("draft_process_save", {"path": PROCESS, "xml": 5}, xml),
        ("draft_process_save", {"xml": "<x/>"}, wrong_path),
        ("draft_process_save", {"path": "case-types/loan.json", "xml": "<x/>"}, wrong_path),
        ("draft_process_edit", {"path": PROCESS}, edits),
        ("draft_process_edit", {"path": PROCESS, "edits": []}, edits),
        ("draft_process_edit", {"path": PROCESS, "edits": EDITS * 51}, edits),
        ("draft_process_edit", {"path": PROCESS, "edits": "x"}, edits),
        ("draft_process_edit", {"path": PROCESS, "edits": ["x"]}, edits),
        ("draft_process_edit", {"path": PROCESS, "edits": [{"old": "", "new": "b"}]}, edit.format(n=1)),
        ("draft_process_edit", {"path": PROCESS, "edits": [{"old": "a"}]}, edit.format(n=1)),
        ("draft_process_edit", {"path": PROCESS, "edits": [*EDITS, {"old": "a", "new": 5}]}, edit.format(n=2)),
        ("draft_process_edit", {"edits": EDITS}, wrong_path),
    ):
        assert json.loads(await _call(tool, arguments, designer)) == {"error": error}, (tool, arguments)
    assert fake_keel.requests == []


async def test_fifty_edits_are_sent(fake_keel: _FakeKeel):
    fake_keel.answer(200, SAVED)

    await _call("draft_process_edit", {"path": PROCESS, "edits": EDITS * 50}, _designer())

    assert len(json.loads(fake_keel.requests[0].content)["edits"]) == 50


async def test_a_file_or_edits_over_the_1_mib_keel_takes_are_never_sent(fake_keel: _FakeKeel):
    designer = _designer()

    save = await _call("draft_process_save", {"path": PROCESS, "xml": "x" * 1_048_576}, designer)
    edit = await _call(
        "draft_process_edit", {"path": PROCESS, "edits": [{"old": "a", "new": "x" * 1_048_576}]}, designer,
    )

    assert json.loads(save) == {
        "error": "The file is over the 1 MiB Keel takes: change it with draft_process_edit, a few elements at a time.",
    }
    assert json.loads(edit) == {"error": "The edits are over the 1 MiB Keel takes: send fewer at a time."}
    assert fake_keel.requests == []


async def test_a_file_within_the_1_mib_keel_takes_is_sent_whole(fake_keel: _FakeKeel):
    fake_keel.answer(200, SAVED)
    # Over the 256,000 bytes the door's other routes take, within the 1 MiB a designer's save takes.
    xml = "x" * 1_000_000

    result = json.loads(await _call("draft_process_save", {"path": PROCESS, "xml": xml}, _designer()))

    assert result == SAVED
    assert json.loads(fake_keel.requests[0].content) == {"xml": xml}


async def test_an_edit_keel_refuses_comes_back_in_keels_words(fake_keel: _FakeKeel):
    message = (
        "Edit 1 of 1: the text to replace is not in the file. Nothing was saved: read the file again and copy "
        "the text to replace exactly."
    )
    fake_keel.answer(422, {"code": "invalid-edit", "message": message})

    result = json.loads(await _call("draft_process_edit", {"path": PROCESS, "edits": EDITS}, _designer()))

    assert result == {"error": message, "code": "invalid-edit"}


async def test_a_keel_without_the_process_routes_is_named_by_a_save_and_an_edit(fake_keel: _FakeKeel):
    fake_keel.answer(404, {"code": "not-found", "message": "Not Found"})

    for tool, arguments in (
        ("draft_process_save", {"path": PROCESS, "xml": "<x/>"}),
        ("draft_process_edit", {"path": PROCESS, "edits": EDITS}),
    ):
        assert json.loads(await _call(tool, arguments, _designer())) == {"error": NO_PROCESS_ROUTES}, tool


async def test_a_json_tool_given_a_process_file_points_to_the_process_tools(fake_keel: _FakeKeel):
    designer = _designer()

    for tool, arguments in (
        ("draft_read", {"path": PROCESS}),
        ("draft_main", {"path": PROCESS}),
        ("draft_save_file", {"path": PROCESS, "content": {}}),
    ):
        assert json.loads(await _call(tool, arguments, designer)) == {"error": JSON_TOOL_ON_A_PROCESS}, tool
    assert fake_keel.requests == []


async def test_a_json_tool_still_takes_a_file_that_is_not_a_process(fake_keel: _FakeKeel):
    # Pins the guard's width: a path under processes/ that is not a process's file is Keel's to refuse.
    fake_keel.answer(404, {"code": "unknown-file", "message": "No such file."})

    await _call("draft_read", {"path": "processes/notes.txt"}, _designer())
    await _call("draft_read", {"path": "case-types/processes.bpmn"}, _designer())

    assert len(fake_keel.requests) == 2


def test_the_json_tools_say_where_a_process_file_is_handled_and_keep_their_first_sentences():
    described = {
        schema["function"]["name"]: schema["function"]["description"]
        for schema in _registry().get_schemas({"draft_read", "draft_main", "draft_save_file"})
    }

    assert "A process's BPMN file is read with draft_process_read." in described["draft_read"]
    assert "A process's BPMN file is read with draft_process_read." in described["draft_main"]
    assert "A process's BPMN file is changed with draft_process_edit or draft_process_save." in (
        described["draft_save_file"]
    )
    assert described["draft_read"].startswith("Read one of the draft's files: its path and its JSON content.")
    assert described["draft_save_file"].startswith("Save one of the draft's definition files, whole:")


async def test_the_process_tools_never_publish_discard_delete_or_write_what_is_published(fake_keel: _FakeKeel):
    designer = _designer()
    calls = (
        ("draft_process_read", {"path": PROCESS}),
        ("draft_process_read", {"path": PROCESS, "published": True}),
        ("draft_process_save", {"path": PROCESS, "xml": "<x/>"}),
        ("draft_process_edit", {"path": PROCESS, "edits": EDITS}),
        ("draft_process_check", {"path": PROCESS}),
        ("draft_process_reference", {}),
    )
    fake_keel.answer(200, {"path": PROCESS, "xml": XML})

    for tool, arguments in calls:
        before = len(fake_keel.requests)
        await _call(tool, arguments, designer)
        assert len(fake_keel.requests) == before + 1, tool

    prefix = f"/api/harness/sessions/{designer.id}/"
    assert {(request.method, request.url.path.removeprefix(prefix)) for request in fake_keel.requests} == {
        ("GET", "draft/process"), ("GET", "draft/main/process"), ("PUT", "draft/process"),
        ("POST", "draft/process/edit"), ("POST", "draft/process/check"), ("GET", "draft/process/reference"),
    }


async def test_no_log_line_carries_a_process_file(fake_keel: _FakeKeel, caplog: pytest.LogCaptureFixture):
    caplog.set_level("DEBUG")
    marker = "PROCESS-TEXT-5521"
    fake_keel.answer(200, {"path": PROCESS, "xml": f"<x>{marker}</x>"})
    designer = _designer()

    await _call("draft_process_save", {"path": PROCESS, "xml": f"<x>{marker}</x>"}, designer)
    await _call("draft_process_edit", {"path": PROCESS, "edits": [{"old": marker, "new": marker}]}, designer)
    await _call("draft_process_read", {"path": PROCESS}, designer)
    await _call("draft_process_check", {"path": PROCESS}, designer)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert marker not in logged
    assert PROCESS not in logged
