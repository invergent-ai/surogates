"""A person's case agent reads its case, and drafts into the person's form, through Keel's door.

The case agent never reads the case's workspace: each of its ``keel_case``
tools asks Keel, under the session's id, and Keel answers as the person.
Every item Keel gives carries a ``cite`` label the agent cites.  A file comes
back as text: a document is converted, plain text decoded, and anything else
refused.  No other session reaches Keel through these tools.
"""
from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from docx import Document

from surogates.keel.client import use_keel_client
from surogates.session.models import Session
from tests.test_case_step_isolation import CONTRACT
from tests.test_case_step_tools import (  # noqa: F401 - fake_keel is a fixture
    _FakeKeel,
    _registry,
    _step,
    fake_keel,
)

CASE = str(uuid4())
REF = {"case_id": CASE, "case_number": "LD-000001", "person_id": str(uuid4())}
CASE_TOOLS = {
    "case_read": {},
    "case_history": {},
    "case_files": {},
    "case_file_read": {"path": "in/application.docx"},
    "case_draft_form": {"token": "t1", "values": {"amount": "12000"}, "sources": [{"file": "in/application.docx"}]},
}
CASE_READ = {
    "case": {"number": "LD-000001", "title": "Loan for Ioana", "type_name": "Loan", "stage": "Review", "status": "open"},
    "data": [{"field": "amount", "label": "Amount", "value": "12000", "cite": "Data: Amount"}],
    "open_steps": [],
}
CASE_HISTORY = {
    "entries": [{"seq": 1, "at": "2026-10-25T09:00:00+00:00", "sentence": "The case was opened.", "cite": "History #1"}],
    "explanation": "What happened on the case, oldest first.",
}
CASE_FILES = {
    "files": [{
        "path": "in/application.docx", "name": "application.docx", "added_by": "Ioana",
        "at": "2026-10-25T09:00:00+00:00", "cite": "application.docx",
    }],
}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _case_agent() -> Session:
    """A person's case agent session, as the Keel link makes one."""
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=None, service_account_id=uuid4(), org_id=uuid4(), agent_id="loan-agent",
        channel="api", status="active",
        config={"memory_boundary": f"case:{CASE}", "case_agent": REF, "channel": "api"},
        created_at=now, updated_at=now,
    )


async def _call(name: str, arguments: dict, session: Session) -> str:
    return await _registry().dispatch(
        name, arguments,
        session_id=str(session.id), session_config=deepcopy(session.config), tool_call_id="c1",
    )


def _route(session: Session, route: str) -> str:
    return f"/api/harness/sessions/{session.id}{route}"


def _file(path: str, content: bytes, media_type: str) -> dict:
    """Keel's answer to a file read."""
    return {
        "path": path, "media_type": media_type,
        "content_base64": base64.b64encode(content).decode(), "cite": path.rsplit("/", 1)[-1],
    }


def _docx(paragraph: str) -> bytes:
    document = Document()
    document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(("tool", "route", "answer"), [
    ("case_read", "/case", CASE_READ),
    ("case_history", "/case/history", CASE_HISTORY),
    ("case_files", "/case/files", CASE_FILES),
])
async def test_each_case_read_goes_to_keels_door_for_this_session(fake_keel: _FakeKeel, tool, route, answer):
    agent = _case_agent()
    fake_keel.answer(200, answer)

    result = await _call(tool, {}, agent)

    [request] = fake_keel.requests
    assert request.method == "GET"
    assert request.url.path == _route(agent, route)
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(result) == answer


async def test_case_file_read_turns_a_document_into_text_with_its_cite(fake_keel: _FakeKeel):
    agent = _case_agent()
    fake_keel.answer(200, _file("in/application.docx", _docx("Monthly income: 4,200"), DOCX))

    result = json.loads(await _call("case_file_read", {"path": "in/application.docx"}, agent))

    [request] = fake_keel.requests
    assert request.method == "GET"
    assert request.url.path == _route(agent, "/case/file")
    assert dict(request.url.params) == {"path": "in/application.docx"}
    assert set(result) == {"path", "cite", "text", "truncated"}
    assert result["path"] == "in/application.docx"
    assert result["cite"] == "application.docx"
    assert "Monthly income: 4,200" in result["text"]
    assert result["truncated"] is False


async def test_case_file_read_refuses_what_is_not_text_and_cuts_what_is_long(fake_keel: _FakeKeel):
    agent = _case_agent()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(256))
    statement = ("Salary paid on the 25th of each month.\n" * 7000)[:250_000]
    scan = _file("in/scan.png", png, "image/png")
    fake_keel.answer(200, scan)
    fake_keel.answer(413, {"code": "too-large", "message": "The file is over 8 MiB."})
    fake_keel.answer(200, _file("in/statement.txt", statement.encode(), "text/plain"))

    refused = await _call("case_file_read", {"path": "in/scan.png"}, agent)
    too_large = json.loads(await _call("case_file_read", {"path": "in/archive.zip"}, agent))
    long = json.loads(await _call("case_file_read", {"path": "in/statement.txt"}, agent))

    assert json.loads(refused) == {"error": "scan.png is neither a document nor text: case_file_read cannot read it."}
    assert scan["content_base64"] not in refused
    assert too_large == {"error": "The file is larger than the 8 MiB Keel sends."}
    assert long["path"] == "in/statement.txt"
    assert long["text"] == statement[:200_000]
    assert long["truncated"] is True


async def test_a_file_whose_json_outgrows_the_result_comes_back_whole_and_cut(fake_keel: _FakeKeel):
    agent = _case_agent()
    # 200,000 characters, each escaped to two in JSON.
    dense = '"\n' * 100_000
    fake_keel.answer(200, _file("in/quotes.txt", dense.encode(), "text/plain"))

    result = json.loads(await _call("case_file_read", {"path": "in/quotes.txt"}, agent))

    assert result["path"] == "in/quotes.txt"
    assert result["truncated"] is True
    assert 0 < len(result["text"]) < len(dense)
    assert dense.startswith(result["text"])


async def test_a_long_case_comes_back_whole(fake_keel: _FakeKeel):
    agent = _case_agent()
    entry = CASE_HISTORY["entries"][0]
    history = {
        "entries": [{**entry, "seq": seq, "cite": f"History #{seq}"} for seq in range(1, 1500)],
        "explanation": CASE_HISTORY["explanation"],
    }
    fake_keel.answer(200, history)

    assert json.loads(await _call("case_history", {}, agent)) == history


async def test_case_draft_form_drafts_into_the_persons_form(fake_keel: _FakeKeel):
    agent = _case_agent()
    draft = CASE_TOOLS["case_draft_form"]
    problems = [{"field": "colour", "message": "The form has no such field."}]
    fake_keel.answer(200, {"drafted": ["amount"]})
    fake_keel.answer(409, {"code": "not-yours", "message": "You hold no open step on this token."})
    fake_keel.answer(409, {"code": "unknown-field", "message": "A field is not on the form.", "problems": problems})

    drafted = await _call("case_draft_form", draft, agent)
    not_yours = json.loads(await _call("case_draft_form", draft, agent))
    unknown = json.loads(await _call("case_draft_form", {**draft, "values": {"colour": "red"}}, agent))

    first = fake_keel.requests[0]
    assert first.method == "POST"
    assert first.url.path == _route(agent, "/case/form-draft")
    assert json.loads(first.content) == {
        "token": "t1", "values": {"amount": "12000"}, "sources": [{"file": "in/application.docx"}],
    }
    # Keel's answer as it came: Keel's chat reads this content.
    assert json.loads(drafted) == {"drafted": ["amount"]}
    assert not_yours == {"error": "You hold no open step on this token.", "code": "not-yours"}
    assert unknown == {"error": "A field is not on the form.", "code": "unknown-field", "problems": problems}

    # A draft with no source for its values is never sent.
    unsourced = json.loads(await _call("case_draft_form", {**draft, "sources": []}, agent))
    assert "error" in unsourced
    assert len(fake_keel.requests) == 3


async def test_a_draft_keel_would_refuse_whole_is_never_sent(fake_keel: _FakeKeel):
    agent = _case_agent()
    draft = CASE_TOOLS["case_draft_form"]

    too_large = json.loads(await _call("case_draft_form", {**draft, "values": {"notes": "x" * 300_000}}, agent))
    not_json = json.loads(await _call("case_draft_form", {**draft, "values": {"amount": float("nan")}}, agent))

    assert too_large["error"].startswith("The draft is ")
    assert too_large["error"].endswith(
        "bytes, over the 256,000 bytes Keel takes. Shorten the values and their sources and call case_draft_form again.",
    )
    assert not_json["error"].startswith("The draft holds a number JSON cannot carry (NaN or Infinity).")
    assert fake_keel.requests == []


@pytest.mark.parametrize(("status", "body", "shown"), [
    (429, {"code": "rate-limited", "message": "Slow down."}, {"error": "Keel could not be reached; call case_read again."}),
    (502, {}, {"error": "Keel could not be reached; call case_read again."}),
    (503, {"code": "harness-off", "message": "The door is off."}, {"error": "The door is off.", "code": "harness-off"}),
    (404, {}, {"error": "Keel refused the call (404).", "code": None}),
], ids=["429", "502", "harness-off", "404-without-a-message"])
async def test_keels_other_answers(fake_keel: _FakeKeel, status, body, shown):
    fake_keel.answer(status, body)

    assert json.loads(await _call("case_read", {}, _case_agent())) == shown


async def test_a_case_tool_outside_a_case_agent_session_reaches_nothing(fake_keel: _FakeKeel):
    step = _step(deepcopy(CONTRACT))
    chat = _case_agent().model_copy(update={"config": {"channel": "web"}})

    for session in (step, chat):
        for tool, arguments in CASE_TOOLS.items():
            result = json.loads(await _call(tool, arguments, session))
            assert "error" in result, (tool, session.config)
    assert fake_keel.requests == []


async def test_keel_unreachable_or_absent(fake_keel: _FakeKeel):
    agent = _case_agent()
    fake_keel.unreachable()

    unreachable = json.loads(await _call("case_read", {}, agent))
    use_keel_client(None)
    absent = json.loads(await _call("case_read", {}, agent))

    assert unreachable == {"error": "Keel could not be reached; call case_read again."}
    assert absent == {"error": "This runtime has no link to Keel."}
