"""The two tools that end a case's step, and the end of the turn they make.

A step session reports its step to Keel's door: ``complete_step`` with one of
the contract's outcomes, or ``raise_hand`` when it cannot finish.  Keel's
answer decides what the session does next:

- an accepted report, or a step that is no longer the session's, ends the turn;
- a rejection is an error the model fixes, and the turn goes on;
- a refusal of the report itself stops the step, Keel is told it failed, and
  people take the step;
- an unreachable Keel is called again.
"""
from __future__ import annotations

import hashlib
import json
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
from surogates.harness.loop import _case_step_ended
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
        redacted = _redacted_case_config(self.row.config)
        if redacted is not None:
            self.row.config = redacted

    def redacted(self) -> bool:
        step = self.row.config["case_step"]
        return step["inputs"] == {} and step.get("inputs_redacted") is True


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


REJECTION = {"code": "missing-sources", "message": "loan-agent gave no sources for income", "attempts_left": 2}


@pytest.mark.parametrize(("door", "ends"), [
    ((200, ACCEPTED), "ended"),
    ((409, {"code": "not-running", "message": "The step has moved on."}), "ended"),
    ((409, REJECTION), "rejected"),
    ((401, {"code": "unauthenticated", "message": "Wrong key."}), "stopped"),
    ((409, {"code": "broken-definition", "message": "The case type cannot run."}), "stopped"),
    ((503, {"code": "harness-off", "message": "The door is off."}), "stopped"),
    ((503, {"code": "unavailable", "message": "Try again."}), "retry"),
    ((500, {}), "retry"),
    ((429, {"code": "slow-down", "message": "Too many."}), "retry"),
    (None, "retry"),
], ids=["accepted", "not-running", "rejected", "401", "broken-definition", "harness-off", "503", "500", "429", "unreachable"])
async def test_the_door_answers_map_to_a_turn_end_or_a_retry(fake_keel, store, door, ends):
    if door is None:
        fake_keel.unreachable()
    else:
        fake_keel.answer(*door)

    result = await _call("complete_step", COMPLETION, store)

    ended = fake_keel.bodies("/ended")
    if ends == "ended":
        assert result.startswith("Step ended:")
        assert ended == []
    elif ends == "rejected":
        assert json.loads(result) == {"error": REJECTION["message"], "code": "missing-sources"}
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
        "evidence": [{"file": "in/payslips/03.pdf", "employer": "first"}],
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

    for bad in (
        {"question": ""},
        {"question": "x" * 2001},
        {"question": "Which?", "choices": [f"choice {n}" for n in range(21)]},
        {"question": "Which?", "choices": ["x" * 201]},
        {"question": "Which?", "evidence": ["a payslip"]},
        {"question": "Which?", "evidence": [{"n": n} for n in range(51)]},
    ):
        assert "error" in json.loads(await _call("raise_hand", bad, store)), bad
    assert len(fake_keel.requests) == 1


def _calls(name: str, arguments: dict, call_id: str = "c1"):
    """The model's answer calling *name*."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


def _sandbox_without_step_tools() -> MagicMock:
    """A sandbox that knows no step tool, as the real one does not."""
    pool = MagicMock(spec=SandboxPool)
    pool.ensure = AsyncMock()
    pool.execute = AsyncMock(side_effect=lambda _owner, name, _args: json.dumps({"error": f"Unknown tool: {name}"}))
    pool.release_for_session = AsyncMock(return_value=None)
    pool.holds_copy = MagicMock(return_value=False)
    return pool


async def _wake(monkeypatch, store: _StepStore, replies: list) -> list[dict]:
    """One wake of the step, the model answering with *replies* in turn: the requests it was sent."""
    sent: list[dict] = []
    script = iter(replies)

    async def llm(**call):
        sent.append(deepcopy(call["create_kwargs"]))
        try:
            return next(script)
        except StopIteration:
            raise AssertionError("the model was called more often than scripted") from None

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    harness = _make_harness(
        session_store=store,
        tool_registry=_registry(),
        budget=IterationBudget(max_total=6),
        context_compressor=SimpleNamespace(
            context_length=1000, _context_window=200_000, should_compress=lambda *a, **k: False,
        ),
        prompt_builder=SimpleNamespace(has_agents=False),
        sandbox_pool=_sandbox_without_step_tools(),
    )
    harness._storage = store.storage
    harness._streaming_enabled = False
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)

    kickoff = render_kickoff(step_contract_in(store.row.config))
    store.write(EventType.USER_MESSAGE, {"content": kickoff, "synthetic": "keel_step"})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": kickoff}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    return sent


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


def test_a_step_ending_result_ends_the_turn_after_its_batch():
    calls = [
        {"id": "c0", "function": {"name": "write_file"}},
        {"id": "c1", "function": {"name": "complete_step"}},
    ]
    written = {"tool_call_id": "c0", "content": "Wrote out/offer.pdf"}

    assert _case_step_ended(calls, [written, {"tool_call_id": "c1", "content": "Step ended: Keel completed it."}])
    assert _case_step_ended(calls, [written, {"tool_call_id": "c1", "content": "Step stopped: Keel refused it."}])
    hand = [{"id": "c2", "function": {"name": "raise_hand"}}]
    assert _case_step_ended(hand, [{"tool_call_id": "c2", "content": "Step ended: A person will answer."}])
    refused = json.dumps({"error": "loan-agent gave no sources for income", "code": "missing-sources"})
    assert not _case_step_ended(calls, [written, {"tool_call_id": "c1", "content": refused}])
    # Only a step tool's result ends the step.
    assert not _case_step_ended(calls[:1], [{"tool_call_id": "c0", "content": "Step ended: a file's own words"}])


async def test_an_accepted_completion_ends_the_wake(fake_keel, monkeypatch, store):
    sent = await _wake(monkeypatch, store, [_calls("complete_step", COMPLETION)])

    assert len(sent) == 1
    assert _ended_with(store) == "case_step_ended"
    assert store.row.status == "completed"
    assert len(fake_keel.bodies("/complete")) == 1


async def test_a_message_that_arrived_meanwhile_keeps_the_turn_going(fake_keel, monkeypatch, store):
    fake_keel.answer(200, ACCEPTED)
    fake_keel.answer(409, {"code": "not-running", "message": "The step has moved on."})

    def a_person_writes_during_the_first_report() -> None:
        if len(fake_keel.requests) == 1:
            store.write(EventType.USER_MESSAGE, {"content": "Also check the co-applicant's payslips."})

    fake_keel.during = a_person_writes_during_the_first_report

    sent = await _wake(monkeypatch, store, [
        _calls("complete_step", COMPLETION, "c1"),
        _calls("complete_step", COMPLETION, "c2"),
    ])

    assert len(sent) == 2
    assert "co-applicant" in json.dumps(sent[1]["messages"])
    assert _ended_with(store) == "case_step_ended"


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
