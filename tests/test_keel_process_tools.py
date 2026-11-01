"""A person's case agent on a process instance reads it, and drafts into a task's form, through Keel's door.

The tools ask Keel under the session's id, as the person, and answer Keel's JSON as it came.  They read, and
they draft values: none completes a task, chooses an outcome, retries, cancels, moves or sends.  Only a case
agent whose reference names the subject ``process`` reaches them.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from surogates.keel.case_agents import case_agent_in, is_process_agent_config
from surogates.runtime.governance import KEEL_PROCESS_TOOLS
from surogates.session.models import Session
from surogates.tools.builtin import keel_case
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
from tests.test_case_step_isolation import CONTRACT
from tests.test_case_step_tools import (  # noqa: F401 - fake_keel is a fixture
    _FakeKeel,
    _registry,
    _step,
    fake_keel,
)

INSTANCE = str(uuid4())
REF = {"case_id": INSTANCE, "case_number": "P-000001", "person_id": str(uuid4()), "subject": "process"}
PROCESS_TOOLS = {
    "process_read": {},
    "process_history": {},
    "process_waits": {},
    "process_move_check": {},
    "process_draft_form": {"task": "t3", "values": {"note": "Looks fine"}, "sources": [{"cite": "Data: Amount"}]},
}
PROCESS_READ = {
    "process": {"number": "P-000001", "name": "Loan", "version": 2, "status": "running", "error": None},
    "data": [{"field": "amount", "label": "Amount", "value": 12000, "cite": "Data: Amount"}],
    "open_tasks": [],
    "incidents": [],
}


def _process_agent() -> Session:
    """A person's case agent session on a process instance, as the Keel link makes one."""
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=None, service_account_id=uuid4(), org_id=uuid4(), agent_id="loan-agent",
        channel="api", status="active",
        config={"memory_boundary": f"case:{INSTANCE}", "case_agent": REF, "channel": "api"},
        created_at=now, updated_at=now,
    )


def _case_agent() -> Session:
    """A person's case agent on a case: its reference holds no subject."""
    agent = _process_agent()
    ref = {key: value for key, value in REF.items() if key != "subject"}
    return agent.model_copy(update={"config": {**agent.config, "case_agent": ref}})


async def _call(name: str, arguments: dict, session: Session) -> str:
    return await _registry().dispatch(
        name, arguments,
        session_id=str(session.id), session_config=deepcopy(session.config), tool_call_id="c1",
    )


def _route(session: Session, route: str) -> str:
    return f"/api/harness/sessions/{session.id}{route}"


@pytest.mark.parametrize(("tool", "route"), [
    ("process_read", "/process"),
    ("process_history", "/process/history"),
    ("process_waits", "/process/waits"),
    ("process_move_check", "/process/move"),
])
async def test_each_process_read_goes_to_keels_door_for_this_session(fake_keel: _FakeKeel, tool, route):
    agent = _process_agent()
    fake_keel.answer(200, PROCESS_READ)

    result = await _call(tool, {}, agent)

    [request] = fake_keel.requests
    assert request.method == "GET"
    assert request.url.path == _route(agent, route)
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(result) == PROCESS_READ


async def test_process_draft_form_drafts_values_into_the_persons_form(fake_keel: _FakeKeel):
    agent = _process_agent()
    draft = PROCESS_TOOLS["process_draft_form"]
    fake_keel.answer(200, {"drafted": ["note"]})
    fake_keel.answer(409, {"code": "not-yours", "message": "Your person holds no open item of this task."})

    drafted = await _call("process_draft_form", draft, agent)
    not_yours = json.loads(await _call("process_draft_form", draft, agent))

    first = fake_keel.requests[0]
    assert first.method == "POST"
    assert first.url.path == _route(agent, "/process/form-draft")
    assert json.loads(first.content) == {
        "task": "t3", "values": {"note": "Looks fine"}, "sources": [{"cite": "Data: Amount"}],
    }
    # Keel's answer as it came: Keel's chat reads this content.
    assert json.loads(drafted) == {"drafted": ["note"]}
    assert not_yours == {"error": "Your person holds no open item of this task.", "code": "not-yours"}


async def test_a_draft_that_names_an_outcome_is_never_sent(fake_keel: _FakeKeel):
    agent = _process_agent()

    result = json.loads(await _call("process_draft_form", {**PROCESS_TOOLS["process_draft_form"], "outcome": "approve"}, agent))

    assert result == {"error": "process_draft_form drafts values only: the outcome is the person's to choose."}
    assert fake_keel.requests == []


@pytest.mark.parametrize(("change", "error"), [
    ({"task": ""}, "task must be the open task's id, as process_read gives it."),
    ({"task": 3}, "task must be the open task's id, as process_read gives it."),
    ({"values": ["note"]}, "values must be an object of form field to value."),
    ({"sources": []}, "sources must be a non-empty list of objects, each naming where the values come from."),
])
async def test_a_draft_keel_would_refuse_is_never_sent(fake_keel: _FakeKeel, change, error):
    agent = _process_agent()

    result = json.loads(await _call("process_draft_form", {**PROCESS_TOOLS["process_draft_form"], **change}, agent))

    assert result == {"error": error}
    assert fake_keel.requests == []


async def test_a_process_tool_outside_a_process_agent_session_reaches_nothing(fake_keel: _FakeKeel):
    step = _step(deepcopy(CONTRACT))
    chat = _process_agent().model_copy(update={"config": {"channel": "web"}})

    for session in (_case_agent(), step, chat):
        for tool, arguments in PROCESS_TOOLS.items():
            result = json.loads(await _call(tool, arguments, session))
            assert result == {
                "error": "This session is no person's case agent on a process instance: "
                "it has no instance to read through Keel.",
            }, (tool, session.config)
    assert fake_keel.requests == []


async def test_a_case_tool_in_a_process_agent_session_reaches_nothing(fake_keel: _FakeKeel):
    agent = _process_agent()

    for tool, arguments in {
        "case_read": {}, "case_history": {}, "case_files": {},
        "case_file_read": {"path": "in/application.docx"},
        "case_draft_form": {"token": "t1", "values": {"amount": "12000"}, "sources": [{"file": "in/application.docx"}]},
    }.items():
        assert json.loads(await _call(tool, arguments, agent)) == {"error": keel_case._NOT_A_CASE_AGENT}, tool
    assert fake_keel.requests == []


async def test_the_process_tools_only_read_and_draft(fake_keel: _FakeKeel):
    agent = _process_agent()
    fake_keel.answer(200, PROCESS_READ)

    for tool, arguments in PROCESS_TOOLS.items():
        await _call(tool, arguments, agent)

    assert KEEL_PROCESS_TOOLS == {
        "process_read", "process_history", "process_waits", "process_move_check", "process_draft_form",
    }
    assert [(request.method, request.url.path.removeprefix(_route(agent, ""))) for request in fake_keel.requests] == [
        ("GET", "/process"), ("GET", "/process/history"), ("GET", "/process/waits"), ("GET", "/process/move"),
        ("POST", "/process/form-draft"),
    ]


@pytest.mark.parametrize("tool", sorted(PROCESS_TOOLS))
def test_the_process_tools_run_in_the_harness(tool):
    assert TOOL_LOCATIONS[tool] is ToolLocation.HARNESS


def test_a_session_without_a_subject_is_a_case_session():
    without = {key: value for key, value in REF.items() if key != "subject"}
    config = {"memory_boundary": f"case:{INSTANCE}", "case_agent": without}

    assert case_agent_in(config).subject == "case"
    assert not is_process_agent_config(config)
    # A key this runtime does not know is ignored, as before.
    assert case_agent_in({**config, "case_agent": {**without, "x": 1}}).subject == "case"
    assert is_process_agent_config({**config, "case_agent": REF})
    # No case boundary, no case agent.
    assert not is_process_agent_config({"case_agent": REF})
    assert not is_process_agent_config(None)
