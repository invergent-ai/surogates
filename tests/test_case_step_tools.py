"""The two tools that end a case's step, and the end of the turn they make.

A step session reports its step to Keel's door: ``complete_step`` with one of
the contract's outcomes, or ``raise_hand`` when it cannot finish.  Keel's
answer decides what the session does next:

- an accepted report, or a step that is no longer the session's, ends the turn;
- a rejection is an error the model fixes, and the turn goes on;
- a refusal of the report itself stops the step, Keel is told it failed, and
  people take the step;
- an unreachable Keel is called again.

Keel's word to the step, an answer or a revision, that lands while the step
reports keeps the turn going: the model reads it, and the step is the
session's again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.keel.client import KeelClient, keel_client, use_keel_client
from surogates.keel.steps import render_kickoff, step_contract_in
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.store import _redacted_case_config
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import boundary_workspace_key
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_case_step_isolation import CONTRACT
from tests.test_harness_resilience import _make_harness
from tests.test_outcome_harness import FakeStore

CASE = CONTRACT["case_id"]
BUCKET = "case-workspaces"
PAGE = {"kb_id": "kb-uuid", "path": "policies/income.md", "sha256": "a" * 64}
OTHER_PAGE = {"kb_id": "kb-uuid", "path": "policies/applicants.md", "sha256": "b" * 64}
ACCEPTED = {"result": "completed", "message": "Keel completed the step."}
COMPLETION = {
    "outcome": "complete",
    "output": {"docs_complete": True, "income": 5200},
    "sources": {
        "docs_complete": [{"file": "in/checklist.pdf"}],
        "income": [{"kb_id": "kb-uuid", "path": "policies/income.md"}],
    },
}


class _FakeKeel:
    """Keel's door.  Each answer it is given is used once, in order, and the last for every request after."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._answers: list[tuple[int, dict]] = [(200, ACCEPTED)]
        self._told = False
        self._down = False
        # Called while a request is answered: what else happens meanwhile.
        self.during = None

    def answer(self, status: int, body: dict) -> None:
        if not self._told:
            self._answers, self._told = [], True
        self._answers.append((status, body))
        self._down = False

    def unreachable(self) -> None:
        self._down = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.during is not None:
            self.during()
        if self._down:
            raise httpx.ConnectError("connection refused", request=request)
        status, body = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return httpx.Response(status, json=body)

    def bodies(self, route: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(route)]


class _StepStore(FakeStore):
    """``FakeStore`` with the step's session row, its whole event log, and its workspace."""

    def __init__(self, session: Session, storage: LocalBackend) -> None:
        super().__init__()
        self.row = session
        self.storage = storage
        self.log: list[SimpleNamespace] = []
        self.redactions = 0

    async def emit_event(self, session_id, event_type, data, *, lease_token=None) -> int:
        event_id = await super().emit_event(session_id, event_type, data, lease_token=lease_token)
        self.log.append(SimpleNamespace(id=event_id, session_id=session_id, type=event_type.value, data=data))
        return event_id

    def write(self, event_type: EventType, data: dict) -> SimpleNamespace:
        """An event another writer adds to the step's log."""
        event = SimpleNamespace(id=self.next_event_id, session_id=self.row.id, type=event_type.value, data=data)
        self.next_event_id += 1
        self.log.append(event)
        return event

    async def put(self, path: str, data: bytes) -> None:
        """A file the step's sessions wrote to the case's workspace."""
        await self.storage.write(BUCKET, boundary_workspace_key(self.row.config, self.row, str(self.row.id), path), data)

    async def get_session(self, session_id):
        assert session_id == self.row.id
        return self.row.model_copy(deep=True)

    async def get_events(self, session_id, *, after=None, limit=None, types=None, exclude_types=None):
        wanted = {t.value for t in types} if types else None
        return [
            e for e in self.log
            if e.session_id == session_id and (after is None or e.id > after) and (wanted is None or e.type in wanted)
        ]

    async def update_session_status(self, session_id, status) -> None:
        self.row.status = status

    async def redact_case_inputs(self, session_id) -> None:
        self.redactions += 1
        redacted = _redacted_case_config(self.row.config)
        if redacted is not None:
            self.row.config = redacted

    async def last_event_id(self, session_id) -> int:
        assert session_id == self.row.id
        return self.next_event_id - 1

    async def mark_case_step_reported(self, session_id, result, *, since=None) -> None:
        # Not over a message of Keel's that revived the step after the report began.
        if since is not None and any(
            e.id > since and e.type == EventType.USER_MESSAGE.value
            and e.data.get("synthetic") in ("keel_answer", "keel_revision")
            for e in self.log
        ):
            return
        self.row.config = {**self.row.config, "case_step": {**self.row.config["case_step"], "reported": result}}

    def keel_writes(self, kind: str, content: str) -> None:
        """Keel's answer or revision reaching the step through the link: a session at rest set active with a
        ``session.resume`` first, the message written, the report's mark cleared."""
        if self.row.status in ("completed", "paused", "failed"):
            self.write(EventType.SESSION_RESUME, {})
            self.row.status = "active"
        self.write(EventType.USER_MESSAGE, {"content": content, "synthetic": f"keel_{kind}", "keel_key": f"keel:{kind}:{content}"})
        self.row.config = {
            **self.row.config, "case_step": {k: v for k, v in self.row.config["case_step"].items() if k != "reported"},
        }

    def redacted(self) -> bool:
        step = self.row.config["case_step"]
        return step["inputs"] == {} and step.get("inputs_redacted") is True

    def reported(self) -> str | None:
        """What the report that ended the step for this round came to, as the session's row holds it."""
        return self.row.config["case_step"].get("reported")

    def result(self, call_id: str) -> str:
        """What the tool call *call_id* answered the model."""
        [data] = [d for _, kind, d in self.events if kind is EventType.TOOL_RESULT and d["tool_call_id"] == call_id]
        return data["content"]


class _QueuedStore(_StepStore):
    """The step's store as a worker's wake reads it: the session's status, the harness's cursor, and the lease.

    The cursor never moves back, as the store's does not.
    """

    def __init__(self, session: Session, storage: LocalBackend) -> None:
        super().__init__(session, storage)
        self.cursor = 0

    async def get_harness_cursor(self, session_id) -> int:
        assert session_id == self.row.id
        return self.cursor

    async def advance_harness_cursor(self, session_id, through_event_id, lease_token, *, rests_with=None) -> bool:
        await super().advance_harness_cursor(session_id, through_event_id, lease_token)
        self.cursor = max(self.cursor, through_event_id)
        if rests_with is None or self.row.status != "active":
            return False
        self.row.status = "completed"
        await self.emit_event(session_id, EventType.SESSION_COMPLETE, rests_with)
        return True

    async def try_acquire_lease(self, session_id, worker_id, ttl_seconds=None) -> SimpleNamespace:
        return SimpleNamespace(lease_token=uuid4())

    async def renew_lease(self, session_id, lease_token, ttl_seconds=None) -> bool:
        return True

    async def release_lease(self, session_id, lease_token) -> None:
        return None

    def message_of_keel(self, content: str) -> SimpleNamespace:
        [message] = [e for e in self.log if e.type == EventType.USER_MESSAGE.value and e.data["content"] == content]
        return message


def _step(contract: dict) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=None, service_account_id=uuid4(), org_id=uuid4(), agent_id="loan-agent",
        channel="api", status="active",
        config={
            "case_step": contract,
            "case_scope": {"tools": None, "knowledge": ["kb-uuid"]},
            "memory_boundary": f"case:{CASE}",
            "workspace_boundary": f"case:{CASE}",
            "storage_bucket": BUCKET,
        },
        input_tokens=1200, output_tokens=340, estimated_cost_usd=Decimal("0.0123"),
        created_at=now, updated_at=now,
    )


@pytest.fixture
def fake_keel():
    keel = _FakeKeel()
    previous = keel_client()
    use_keel_client(KeelClient("http://keel-door.test", "door-key", transport=httpx.MockTransport(keel.handle)))
    yield keel
    use_keel_client(previous)


@pytest.fixture
def store(tmp_path) -> _StepStore:
    return _StepStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))


def _store_for(tmp_path, **contract) -> _StepStore:
    return _StepStore(_step({**deepcopy(CONTRACT), **contract}), LocalBackend(str(tmp_path)))


def kwargs(store: _StepStore, tool_call_id: str = "c1") -> dict:
    """What the harness runs a tool of the step's session with."""
    return {
        "session_id": str(store.row.id),
        "session_store": store,
        "session_config": deepcopy(store.row.config),
        "storage": store.storage,
        "tool_call_id": tool_call_id,
    }


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return registry


async def _call(name: str, arguments: dict, store: _StepStore, tool_call_id: str = "c1") -> str:
    return await _registry().dispatch(name, arguments, **kwargs(store, tool_call_id))


async def test_an_accepted_completion_carries_its_call_pages_and_usage(fake_keel, store):
    # Two reads of one page, and another page.
    for page in (PAGE, OTHER_PAGE, PAGE):
        store.write(EventType.KB_PAGE_READ, page)

    result = await _call("complete_step", COMPLETION, store)

    assert result == "Step ended: Keel completed the step."
    [request] = fake_keel.requests
    assert request.method == "POST"
    assert request.url.path == f"/api/harness/sessions/{store.row.id}/complete"
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(request.content) == {
        "call_id": "c1",
        **COMPLETION,
        "pages_read": [OTHER_PAGE, PAGE],
        "usage": {"input_tokens": 1200, "output_tokens": 340, "estimated_cost_usd": "0.0123"},
    }
    assert store.redacted()


async def test_a_page_read_without_a_sha256_digest_is_left_out_of_the_report(fake_keel, store):
    # Keel refuses a whole report whose page carries anything but a SHA-256 hex digest.
    for sha256 in ("legacy", "A" * 64, "a" * 63, "g" * 64, None):
        store.write(EventType.KB_PAGE_READ, {**OTHER_PAGE, "sha256": sha256})
    store.write(EventType.KB_PAGE_READ, PAGE)

    result = await _call("complete_step", COMPLETION, store)

    assert result == "Step ended: Keel completed the step."
    [request] = fake_keel.requests
    assert json.loads(request.content)["pages_read"] == [PAGE]


async def test_a_page_read_beyond_keels_bounds_is_left_out_of_the_report(fake_keel, store):
    # Keel refuses a whole report whose page names a knowledge base or a path it cannot take.
    for page in (
        {**OTHER_PAGE, "path": "policies/" + "x" * 1992},
        {**OTHER_PAGE, "kb_id": "k" * 201},
        {**OTHER_PAGE, "path": ""},
        {**OTHER_PAGE, "kb_id": ""},
    ):
        store.write(EventType.KB_PAGE_READ, page)
    store.write(EventType.KB_PAGE_READ, PAGE)
    at_the_bounds = {"kb_id": "k" * 200, "path": "p" * 2000, "sha256": "c" * 64}
    store.write(EventType.KB_PAGE_READ, at_the_bounds)

    result = await _call("complete_step", COMPLETION, store)

    assert result == "Step ended: Keel completed the step."
    [request] = fake_keel.requests
    assert json.loads(request.content)["pages_read"] == [PAGE, at_the_bounds]


async def test_a_file_field_becomes_a_case_file(fake_keel, tmp_path):
    store = _store_for(tmp_path, files={"offer": "one", "payslips": "list"})
    offer, march, april = b"%PDF-1.7 offer", b"%PDF-1.7 march", b"%PDF-1.7 april"
    await store.put("out/offer.pdf", offer)
    await store.put("in/payslips/03.pdf", march)
    await store.put("in/payslips/04.pdf", april)

    def files(**output) -> dict:
        return {**COMPLETION, "output": {**COMPLETION["output"], **output}}

    result = await _call("complete_step", files(
        offer="out/offer.pdf", payslips=["/workspace/in/payslips/03.pdf", "in/payslips/04.pdf"],
    ), store)

    assert result.startswith("Step ended:")
    [body] = fake_keel.bodies("/complete")
    assert body["output"]["offer"] == {
        "name": "offer.pdf", "uri": f"case-file://{CASE}/out/offer.pdf", "sha256": hashlib.sha256(offer).hexdigest(),
    }
    assert body["output"]["payslips"] == [
        {"name": "03.pdf", "uri": f"case-file://{CASE}/in/payslips/03.pdf", "sha256": hashlib.sha256(march).hexdigest()},
        {"name": "04.pdf", "uri": f"case-file://{CASE}/in/payslips/04.pdf", "sha256": hashlib.sha256(april).hexdigest()},
    ]
    assert body["output"]["docs_complete"] is True

    # A path with no file behind it, outside the workspace, or not the field's shape never reaches Keel.
    for output, error in [
        ({"offer": "out/missing.pdf"}, "offer: no file out/missing.pdf in the workspace"),
        ({"payslips": ["in/payslips/03.pdf", "in/payslips/05.pdf"]}, "payslips: no file in/payslips/05.pdf in the workspace"),
        ({"offer": "../other-case/offer.pdf"}, "offer:"),
        ({"offer": "/etc/passwd"}, "offer:"),
        ({"offer": ["out/offer.pdf"]}, "offer:"),
        ({"payslips": "in/payslips/03.pdf"}, "payslips:"),
    ]:
        result = await _call("complete_step", files(**output), store)
        assert json.loads(result)["error"].startswith(error), output
    assert len(fake_keel.requests) == 1


async def test_an_empty_file_is_a_case_file(fake_keel, store):
    await store.put("out/offer.pdf", b"")

    result = await _call("complete_step", {**COMPLETION, "output": {**COMPLETION["output"], "offer": "out/offer.pdf"}}, store)

    assert result.startswith("Step ended:")
    [body] = fake_keel.bodies("/complete")
    assert body["output"]["offer"] == {
        "name": "offer.pdf", "uri": f"case-file://{CASE}/out/offer.pdf", "sha256": hashlib.sha256(b"").hexdigest(),
    }


async def test_a_report_larger_than_keel_takes_is_never_sent(fake_keel, store):
    long_output = {**COMPLETION, "output": {**COMPLETION["output"], "notes": "x" * 300_000}}

    error = json.loads(await _call("complete_step", long_output, store))["error"]

    size = int(re.match(r"The report is ([\d,]+) bytes, over the 256,000 bytes Keel takes\. ", error)[1].replace(",", ""))
    assert size > 300_000
    assert error.endswith("Shorten the output and its sources and call complete_step again.")
    assert fake_keel.requests == []
    assert not store.redacted()


REJECTION = {"code": "missing-sources", "message": "loan-agent gave no sources for income", "attempts_left": 2}
SHOWN_REJECTION = (
    "loan-agent gave no sources for income "
    "(2 attempts left; fix the report and call complete_step again, or call raise_hand)"
)


@pytest.mark.parametrize(("door", "ends"), [
    ((200, ACCEPTED), "ended"),
    ((200, {"result": "drafted", "message": "A person reviews the draft."}), "ended"),
    ((409, {"code": "not-running", "message": "The step has moved on."}), "ended"),
    ((409, REJECTION), "rejected"),
    ((409, {"code": "invalid-value", "message": "income must be positive"}), "rejected"),
    ((401, {"code": "unauthenticated", "message": "Wrong key."}), "stopped"),
    ((409, {"code": "broken-definition", "message": "The case type cannot run."}), "stopped"),
    ((503, {"code": "harness-off", "message": "The door is off."}), "stopped"),
    ((503, {"code": "unavailable", "message": "Try again."}), "retry"),
    ((500, {}), "retry"),
    ((429, {"code": "slow-down", "message": "Too many."}), "retry"),
    (None, "retry"),
], ids=["accepted", "drafted", "not-running", "rejected", "rejected-uncounted", "401", "broken-definition", "harness-off", "503", "500", "429", "unreachable"])
async def test_the_door_answers_map_to_a_turn_end_or_a_retry(fake_keel, store, door, ends):
    if door is None:
        fake_keel.unreachable()
    else:
        fake_keel.answer(*door)

    result = await _call("complete_step", COMPLETION, store)

    ended = fake_keel.bodies("/ended")
    # A report that ended the step for this round is marked on the session's row with what it came to.
    assert store.reported() == {"ended": door and door[1].get("result"), "stopped": "stopped"}.get(ends)
    if ends == "ended":
        assert result.startswith("Step ended:")
        assert ended == []
        # Only a completion takes the inputs out: a draft resumes here if its reviewer asks for changes.
        assert store.redacted() is (door[1].get("result") == "completed")
    elif ends == "rejected":
        shown = SHOWN_REJECTION if "attempts_left" in door[1] else door[1]["message"]
        assert json.loads(result) == {"error": shown, "code": door[1]["code"]}
        assert ended == []
        assert not store.redacted()
    elif ends == "stopped":
        status = door[0]
        assert result == f"Step stopped: Keel refused this step's report ({status}); people take the step."
        assert ended == [{
            "reason": "failed", "detail": f"keel-{status}",
            "usage": {"input_tokens": 1200, "output_tokens": 340, "estimated_cost_usd": "0.0123"},
        }]
        assert fake_keel.requests[-1].url.path == f"/api/harness/sessions/{store.row.id}/ended"
        assert store.redacted()
    else:
        assert json.loads(result) == {"error": "Keel could not be reached; call complete_step again."}
        assert ended == []
        assert not store.redacted()


async def test_a_runtime_without_keel_stops_the_step(store):
    previous = keel_client()
    use_keel_client(None)
    try:
        result = await _call("complete_step", COMPLETION, store)
    finally:
        use_keel_client(previous)

    assert result.startswith("Step stopped:")
    assert store.redacted()
    assert store.reported() == "stopped"


async def test_an_outcome_outside_the_contract_never_reaches_keel(fake_keel, store):
    for arguments in (
        {**COMPLETION, "outcome": "approved"},
        {**COMPLETION, "output": ["income", 5200]},
        {**COMPLETION, "sources": "the payslips"},
    ):
        result = await _call("complete_step", arguments, store)
        assert "error" in json.loads(result), arguments

    assert "complete, missing" in json.loads(await _call("complete_step", {**COMPLETION, "outcome": "x"}, store))["error"]
    assert fake_keel.requests == []
    assert not store.redacted()


def _with_nan(arguments: dict, path: tuple[str, ...]) -> str:
    """*arguments* as the model's JSON, with a NaN at *path*: Python's JSON reader takes it."""
    arguments = deepcopy(arguments)
    target = arguments
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = float("nan")
    return json.dumps(arguments)


UNREAD = "payslip-of-march.pdf"
LONG_ID = "c" * 201
NOT_JSON = "The report holds a number JSON cannot carry (NaN or Infinity)"
LONG_ID_ERROR = "This tool call's id is longer than the 200 characters Keel takes"


@pytest.mark.parametrize(("name", "arguments", "call_id", "error"), [
    ("complete_step", {**COMPLETION, "sources": {**COMPLETION["sources"], "income": UNREAD}}, "c1",
     "sources.income must be a list of objects"),
    ("complete_step", {**COMPLETION, "sources": {**COMPLETION["sources"], "income": [UNREAD]}}, "c1",
     "sources.income must be a list of objects"),
    ("complete_step", _with_nan(COMPLETION, ("output", "income")), "c1", NOT_JSON),
    ("complete_step", _with_nan(COMPLETION, ("sources", "income", 0, "page")), "c1", NOT_JSON),
    ("complete_step", COMPLETION, LONG_ID, LONG_ID_ERROR),
    ("raise_hand", {"question": "Which?"}, LONG_ID, LONG_ID_ERROR),
], ids=[
    "sources-not-a-list", "sources-not-objects", "nan-in-output", "nan-in-sources",
    "long-call-id", "long-call-id-hand",
])
async def test_a_report_keel_could_not_read_is_never_sent(fake_keel, store, name, arguments, call_id, error):
    result = json.loads(await _call(name, arguments, store, tool_call_id=call_id))["error"]

    assert result.startswith(error)
    assert UNREAD not in result and LONG_ID not in result
    assert fake_keel.requests == []
    assert store.redactions == 0
    assert store.reported() is None


async def test_only_the_steps_own_session_can_report_it(fake_keel, store):
    child_config = {k: v for k, v in store.row.config.items() if k != "case_step"}
    for name, arguments in (("complete_step", COMPLETION), ("raise_hand", {"question": "Which address?"})):
        result = await _registry().dispatch(name, arguments, **{**kwargs(store), "session_config": child_config})
        assert "error" in json.loads(result)
    assert fake_keel.requests == []


async def test_raise_hand_parks_the_step(fake_keel, store):
    fake_keel.answer(200, {"result": "hand-raised", "message": "A person will answer."})
    store.write(EventType.KB_PAGE_READ, PAGE)
    hand = {
        "question": "The payslips name two employers. Which income counts?",
        "choices": ["Both", "The current employer only"],
        "evidence": [{"label": "Employer · March", "value": "the first one", "source": {"file": "in/payslips/03.pdf"}}],
    }

    result = await _call("raise_hand", hand, store, tool_call_id="c7")

    assert result == "Step ended: A person will answer."
    [request] = fake_keel.requests
    assert request.url.path == f"/api/harness/sessions/{store.row.id}/raise-hand"
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(request.content) == {
        "call_id": "c7", **hand, "pages_read": [PAGE],
        "usage": {"input_tokens": 1200, "output_tokens": 340, "estimated_cost_usd": "0.0123"},
    }
    # The step resumes in this session once a person answers: its inputs stay.
    assert not store.redacted()
    assert store.reported() == "hand-raised"

    for bad in (
        {"question": ""},
        {"question": "x" * 2001},
        {"question": "Which?", "choices": [f"choice {n}" for n in range(21)]},
        {"question": "Which?", "choices": ["x" * 201]},
        {"question": "Which?", "evidence": ["a payslip"]},
        {"question": "Which?", "evidence": [{"label": f"payslip {n}", "value": "found"} for n in range(51)]},
    ):
        assert "error" in json.loads(await _call("raise_hand", bad, store)), bad
    assert len(fake_keel.requests) == 1


PAYSLIP = {"label": "Payslip · September", "value": "€6,180 net", "source": {"file": "/workspace/payslips/sep.pdf", "page": 1}}


async def test_evidence_is_label_value_and_an_optional_source(fake_keel, store):
    fake_keel.answer(200, {"result": "hand-raised", "message": "A person will answer."})
    evidence = [
        PAYSLIP,
        {"label": "Employer", "value": "Acme Ltd"},
        {"label": "Contract", "value": "permanent", "source": {"file": "/root/./contracts//acme.pdf"}},
    ]

    result = await _call("raise_hand", {"question": "Which income counts?", "evidence": evidence}, store)

    assert result == "Step ended: A person will answer."
    [body] = fake_keel.bodies("/raise-hand")
    # Each file as the case's files name it: relative to the case's workspace.
    assert body["evidence"] == [
        {"label": "Payslip · September", "value": "€6,180 net", "source": {"file": "payslips/sep.pdf", "page": 1}},
        {"label": "Employer", "value": "Acme Ltd"},
        {"label": "Contract", "value": "permanent", "source": {"file": "contracts/acme.pdf"}},
    ]


@pytest.mark.parametrize(("bad", "index"), [
    ({"value": "€6,180 net", "source": PAYSLIP["source"]}, 0),
    ({**PAYSLIP, "note": "net of tax"}, 1),
    ({**PAYSLIP, "source": {"file": "/workspace/payslips/sep.pdf", "page": 0}}, 2),
    ({**PAYSLIP, "source": {"file": "/etc/passwd"}}, 3),
    ({**PAYSLIP, "source": {"file": "/workspace/payslips/sep.pdf", "sheet": 2}}, 1),
    ({**PAYSLIP, "source": {"file": "/workspace/_history/payslips/sep.pdf"}}, 1),
    ({**PAYSLIP, "source": {"file": "/root/workspace/payslips/sep.pdf"}}, 2),
    ({**PAYSLIP, "source": {"file": "/workspace/../other-case/sep.pdf"}}, 1),
], ids=[
    "no-label", "extra-key", "page-0", "outside-the-workspace", "extra-source-key", "platform-folder",
    "workspace-root-again", "climbs-out",
])
async def test_evidence_outside_the_shape_is_refused(fake_keel, store, bad, index):
    evidence = [PAYSLIP] * index + [bad]

    error = json.loads(await _call("raise_hand", {"question": "Which income counts?", "evidence": evidence}, store))["error"]

    assert error.startswith(f"evidence[{index}]")
    # The refused path is never repeated back.
    assert "sep.pdf" not in error and "passwd" not in error
    assert fake_keel.requests == []
    assert store.reported() is None


def _calls(name: str, arguments: dict, call_id: str = "c1"):
    """The model's answer calling *name*."""
    return _batch((name, arguments, call_id))


def _batch(*calls: tuple[str, dict, str]):
    """The model's answer making *calls*, each a name, its arguments and its id, in one batch."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for name, arguments, call_id in calls
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


def _sandbox(answers: dict[str, str]) -> MagicMock:
    """A sandbox answering its own tools with *answers*, and any other tool as unknown: it knows no step tool."""
    pool = MagicMock(spec=SandboxPool)
    pool.ensure = AsyncMock()
    pool.execute = AsyncMock(
        side_effect=lambda _owner, name, _args: answers.get(name) or json.dumps({"error": f"Unknown tool: {name}"}),
    )
    pool.release_for_session = AsyncMock(return_value=None)
    pool.holds_copy = MagicMock(return_value=False)
    return pool


def _step_harness(
    monkeypatch, store: _StepStore, replies: list, sent: list[dict], *,
    sandbox: dict[str, str] | None = None, streaming: bool = False,
):
    """A worker's harness for the step's session, the model answering with *replies* in turn; *sent* gets each request."""
    script = iter(replies)

    async def llm(**call):
        sent.append(deepcopy(call["create_kwargs"]))
        try:
            reply = next(script)
        except StopIteration:
            raise AssertionError("the model was called more often than scripted") from None
        if isinstance(reply, Exception):
            raise reply
        if call.get("on_tool_call_complete") is not None:
            for tool_call in reply[0].get("tool_calls") or []:
                call["on_tool_call_complete"](tool_call)
            # The rest of the stream, the call's usage last: a call started as it arrived runs meanwhile.
            await asyncio.sleep(0.05)
        return reply

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    harness = _make_harness(
        session_store=store,
        tool_registry=_registry(),
        budget=IterationBudget(max_total=6),
        context_compressor=SimpleNamespace(
            context_length=1000, _context_window=200_000, should_compress=lambda *a, **k: False,
        ),
        prompt_builder=SimpleNamespace(has_agents=False),
        sandbox_pool=_sandbox(sandbox or {}),
    )
    harness._storage = store.storage
    harness._streaming_enabled = streaming
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    return harness


async def _wake(
    monkeypatch, store: _StepStore, replies: list, *, sandbox: dict[str, str] | None = None, streaming: bool = False,
    opening: tuple[EventType, dict, str] | None = None,
) -> list[dict]:
    """One wake of the step, the model answering with *replies* in turn: the requests it was sent.

    A reply that is an exception is the model's call raising it.  *streaming*
    runs the tool calls as a streamed answer's are, each started as it
    arrives.  *opening* is what woke the session: the event written to its
    log (``None`` when it is there already), and the message the model reads
    of it; by default Keel's kickoff for a step, and a person's message for
    any other session.
    """
    sent: list[dict] = []
    harness = _step_harness(monkeypatch, store, replies, sent, sandbox=sandbox, streaming=streaming)

    contract = step_contract_in(store.row.config)
    if opening is not None:
        kind, data, kickoff = opening
        if data is not None:
            store.write(kind, data)
    elif contract is not None:
        kickoff = render_kickoff(contract)
        store.write(EventType.USER_MESSAGE, {"content": kickoff, "synthetic": "keel_step"})
    else:
        kickoff = "What do the payslips say?"
        store.write(EventType.USER_MESSAGE, {"content": kickoff})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": kickoff}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    return sent


async def _queued_wake(
    monkeypatch, store: _QueuedStore, replies: list, *, sandbox: dict[str, str] | None = None, ending=None,
) -> list[dict]:
    """A worker's wake of the step's session, taken from the queue, the model answering with *replies* in turn:
    the requests it was sent.

    The wake reads the session's status, its cursor and its log as a worker
    does.  *ending* is what else happens as a turn of the wake ends, while
    the turn's end detaches the sandbox: after the turn's last look for
    messages, before its end is written.
    """
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    sent: list[dict] = []
    harness = _step_harness(monkeypatch, store, replies, sent, sandbox=sandbox)
    # The prompt and the context are another worker's concern: the wake's own checks run as they are.
    harness._prompt.set_agent_def = lambda _agent_def: None
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._build_system_prompt = AsyncMock(return_value="system")

    async def as_replayed(_session, _events, messages):
        return messages

    harness._engineer_context = as_replayed
    if ending is not None:

        async def detach(_session_id):
            ending()

        harness._sandbox_pool.release_for_session = AsyncMock(side_effect=detach)
    await asyncio.wait_for(harness.wake(store.row.id), 10)
    return sent


def _started(store: _StepStore) -> None:
    """Keel starts the step: its kickoff, in the session's log."""
    store.write(EventType.USER_MESSAGE, {"content": render_kickoff(step_contract_in(store.row.config)), "synthetic": "keel_step"})


def _ended_with(store: _StepStore) -> str:
    kind, data = store.events[-1][1:]
    assert kind is EventType.SESSION_COMPLETE
    return data["reason"]


@pytest.mark.parametrize("mode", ["complete", "draft"])
async def test_the_schema_offers_the_contracts_outcomes_form_and_files(fake_keel, monkeypatch, tmp_path, mode):
    store = _store_for(tmp_path, mode=mode, files={"offer": "one", "payslips": "list"})

    [request] = await _wake(monkeypatch, store, [_calls("complete_step", COMPLETION)])

    tools = {t["function"]["name"]: t["function"] for t in request["tools"]}
    assert "raise_hand" in tools
    completion = tools["complete_step"]
    properties = completion["parameters"]["properties"]
    assert properties["outcome"]["enum"] == ["complete", "missing"]
    output = properties["output"]
    assert output["additionalProperties"] is False
    assert output["properties"]["docs_complete"] == {"type": "boolean"}
    assert output["properties"]["offer"]["type"] == "string"
    assert output["properties"]["payslips"]["type"] == "array"
    assert output["properties"]["payslips"]["items"]["type"] == "string"
    sources = properties["sources"]["properties"]
    assert set(sources) == {"docs_complete", "offer", "payslips"}
    assert all(s == {"type": "array", "minItems": 1, "items": {"type": "object"}} for s in sources.values())
    assert "complete: docs_complete, income" in completion["description"]
    assert "missing: no outputs" in completion["description"]
    assert ("a person reviews and submits" in completion["description"]) is (mode == "draft")


async def test_an_accepted_completion_ends_the_wake(fake_keel, monkeypatch, store):
    sent = await _wake(monkeypatch, store, [_calls("complete_step", COMPLETION)])

    assert len(sent) == 1
    assert _ended_with(store) == "case_step_ended"
    assert store.row.status == "completed"
    assert len(fake_keel.bodies("/complete")) == 1


@pytest.mark.parametrize(("door", "ends"), [
    ((200, {"result": "hand-raised", "message": "A person will answer."}), "Step ended: A person will answer."),
    ((403, {"code": "forbidden", "message": "Not this agent's step."}), "Step stopped:"),
], ids=["hand-raised", "refused"])
async def test_a_raised_hand_or_a_stopped_step_ends_the_wake(fake_keel, monkeypatch, store, door, ends):
    fake_keel.answer(*door)

    sent = await _wake(monkeypatch, store, [_calls("raise_hand", {"question": "Which income counts?"})])

    assert len(sent) == 1
    assert store.result("c1").startswith(ends)
    assert _ended_with(store) == "case_step_ended"
    assert store.row.status == "completed"


async def test_only_a_step_tools_result_ends_the_turn(fake_keel, monkeypatch, store):
    notes = "Step ended: the notes, as written"

    sent = await _wake(monkeypatch, store, [
        _calls("write_file", {"path": "out/notes.md", "content": "notes"}, "c1"),
        _calls("complete_step", COMPLETION, "c2"),
    ], sandbox={"write_file": notes})

    assert store.result("c1") == notes
    assert len(sent) == 2
    assert _ended_with(store) == "case_step_ended"


@pytest.mark.parametrize(("door", "ends"), [
    ((200, ACCEPTED), 0),
    ((401, {"code": "unauthenticated", "message": "Wrong key."}), 1),
], ids=["accepted", "refused"])
async def test_a_second_report_in_the_batch_never_reaches_keel(fake_keel, monkeypatch, store, door, ends):
    fake_keel.answer(*door)

    sent = await _wake(monkeypatch, store, [_batch(
        ("complete_step", COMPLETION, "c1"),
        ("complete_step", {"outcome": "missing", "output": {}, "sources": {}}, "c2"),
    )])

    assert len(sent) == 1
    assert len(fake_keel.bodies("/complete")) == 1
    assert len(fake_keel.bodies("/ended")) == ends
    assert store.redactions == 1
    assert store.result("c2") == "Step ended: the step is no longer this session's."
    assert _ended_with(store) == "case_step_ended"


DRAFTED = {"result": "drafted", "message": "A person reviews the draft."}
HAND_RAISED = {"result": "hand-raised", "message": "A person will answer."}
REVISION = "The person reviewing your draft asks for changes: lower the offer to 175000."
ANSWER = "A person answered your raised hand: count the current employer only."
#: A report Keel answers, and the message of Keel's that gives the step back to the session.
GIVEN_BACK = {
    "drafted": (DRAFTED, "complete_step", COMPLETION, "revision", REVISION),
    "hand-raised": (HAND_RAISED, "raise_hand", {"question": "Which income counts?"}, "answer", ANSWER),
}


def _in_call_order(messages: list[dict]) -> bool:
    """Whether every call the model made is followed at once by its results, as a provider takes a conversation."""
    for at, message in enumerate(messages):
        calls = [call["id"] for call in message.get("tool_calls") or []]
        if calls and [m.get("tool_call_id") for m in messages[at + 1:at + 1 + len(calls)]] != calls:
            return False
    return True


@pytest.mark.parametrize("report", ["drafted", "hand-raised"])
async def test_keels_message_written_while_keel_takes_the_report_keeps_the_turn_going(
    fake_keel, monkeypatch, tmp_path, report,
):
    answer, tool, arguments, kind, content = GIVEN_BACK[report]
    store = _QueuedStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))
    fake_keel.answer(200, answer)

    def keel_writes_during_the_first_report() -> None:
        if len(fake_keel.requests) == 1:
            store.keel_writes(kind, content)

    fake_keel.during = keel_writes_during_the_first_report
    _started(store)

    sent = await _queued_wake(monkeypatch, store, [_calls(tool, arguments, "c1"), _calls(tool, arguments, "c2")])

    # The turn goes on with Keel's message, and the step is the session's again: it reports anew.
    assert len(sent) == 2
    assert content not in json.dumps(sent[0]["messages"])
    assert content in json.dumps(sent[1]["messages"])
    assert [json.loads(r.content)["call_id"] for r in fake_keel.requests] == ["c1", "c2"]
    assert _ended_with(store) == "case_step_ended"
    assert store.row.status == "completed"
    assert store.reported() == report
    # Read in this wake: the wake Keel's message queued finds nothing more.
    assert await _queued_wake(monkeypatch, store, []) == []

    # Keel writes again: the next wake reads the log in the order the turn did.
    fake_keel.during = None
    store.keel_writes(kind, "And once more, please.")
    [request] = await _queued_wake(monkeypatch, store, [_calls(tool, arguments, "c3")])
    assert _in_call_order(request["messages"])
    assert content in json.dumps(request["messages"])
    assert len(fake_keel.requests) == 3


async def test_a_refused_completion_keeps_the_turn_going(fake_keel, monkeypatch, store):
    fake_keel.answer(409, REJECTION)
    fake_keel.answer(200, ACCEPTED)

    sent = await _wake(monkeypatch, store, [
        _calls("complete_step", {**COMPLETION, "sources": {}}, "c1"),
        _calls("complete_step", COMPLETION, "c2"),
    ])

    assert len(sent) == 2
    assert REJECTION["message"] in json.dumps(sent[1]["messages"])
    assert [body["call_id"] for body in fake_keel.bodies("/complete")] == ["c1", "c2"]
    assert _ended_with(store) == "case_step_ended"
    assert store.row.status == "completed"


#: A model with a price, so each call of a test of what a step used costs something.
PRICED = "openai/gpt-5.6-terra"


class _CountingStore(_StepStore):
    """The store of a new step session whose row counts each call of the model as its response is written,
    as the session's row in the database does."""

    def __init__(self, tmp_path) -> None:
        row = _step(deepcopy(CONTRACT)).model_copy(
            update={"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": Decimal(0)},
        )
        super().__init__(row, LocalBackend(str(tmp_path)))

    async def emit_event(self, session_id, event_type, data, *, lease_token=None) -> int:
        if event_type is EventType.LLM_RESPONSE:
            self.row.input_tokens += int(data.get("input_tokens", 0))
            self.row.output_tokens += int(data.get("output_tokens", 0))
            self.row.estimated_cost_usd += Decimal(str(data.get("cost_usd", 0)))
        return await super().emit_event(session_id, event_type, data, lease_token=lease_token)

    def used(self, calls: int | None = None) -> dict:
        """What the model's calls used, by their responses in the log: every call, or the first *calls*."""
        responses = [e.data for e in self.log if e.type == EventType.LLM_RESPONSE.value][:calls]
        cost = sum((Decimal(str(r.get("cost_usd", 0))) for r in responses), Decimal(0))
        return {
            "input_tokens": sum(r["input_tokens"] for r in responses),
            "output_tokens": sum(r["output_tokens"] for r in responses),
            "estimated_cost_usd": format(cost, "f"),
        }


def _using(reply, input_tokens: int, output_tokens: int):
    """*reply*, its call having used *input_tokens* and *output_tokens* of a priced model."""
    message, usage = reply
    return message, {**usage, "model": PRICED, "input_tokens": input_tokens, "output_tokens": output_tokens}


#: How a report reaches Keel: Keel's answer, the tool and its arguments, and the routes the report takes.
REPORTS = {
    "completed": ((200, ACCEPTED), "complete_step", COMPLETION, ["/complete"]),
    "hand-raised": ((200, HAND_RAISED), "raise_hand", {"question": "Which income counts?"}, ["/raise-hand"]),
    "refused": (
        (403, {"code": "forbidden", "message": "Not this agent's step."}), "complete_step", COMPLETION,
        ["/complete", "/ended"],
    ),
}


@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "whole"])
@pytest.mark.parametrize("report", list(REPORTS))
async def test_a_report_carries_what_every_call_of_its_turn_used_its_own_included(
    fake_keel, monkeypatch, tmp_path, report, streaming,
):
    door, tool, arguments, routes = REPORTS[report]
    fake_keel.answer(*door)
    store = _CountingStore(tmp_path)

    await _wake(monkeypatch, store, [
        _using(_calls("read_file", {"path": "in/checklist.pdf"}, "c1"), 1_000, 120),
        _using(_calls(tool, arguments, "c2"), 33_912, 1_787),
    ], sandbox={"read_file": "Checklist: payslips, ID."}, streaming=streaming)

    assert _ended_with(store) == "case_step_ended"
    used = store.used()
    assert used["input_tokens"] == 34_912 and Decimal(used["estimated_cost_usd"]) > 0
    assert [body["usage"] for route in routes for body in fake_keel.bodies(route)] == [used] * len(routes)


async def test_each_report_of_a_wake_carries_what_its_turn_used_through_its_own_call(fake_keel, monkeypatch, tmp_path):
    fake_keel.answer(409, REJECTION)
    fake_keel.answer(200, ACCEPTED)
    store = _CountingStore(tmp_path)

    await _wake(monkeypatch, store, [
        _using(_calls("complete_step", {**COMPLETION, "sources": {}}, "c1"), 30_000, 900),
        _using(_calls("complete_step", COMPLETION, "c2"), 33_912, 1_787),
    ], streaming=True)

    assert [body["usage"] for body in fake_keel.bodies("/complete")] == [store.used(1), store.used(2)]


async def test_a_turn_ending_without_a_report_tells_keel_what_every_call_used(fake_keel, monkeypatch, tmp_path):
    store = _CountingStore(tmp_path)

    await _wake(monkeypatch, store, [
        _using(_calls("read_file", {"path": "in/checklist.pdf"}, "c1"), 1_000, 120),
        _using(({"role": "assistant", "content": "The payslips are there."}, {"finish_reason": "stop"}), 2_000, 80),
        _using(({"role": "assistant", "content": "The documents look complete."}, {"finish_reason": "stop"}), 2_500, 60),
    ], sandbox={"read_file": "Checklist: payslips, ID."}, streaming=True)

    assert [body["usage"] for body in fake_keel.bodies("/ended")] == [store.used()]
