"""The intake agent's ``start_case``: a front door that starts a case of a type Keel grants it.

An agent that case types name as their intake agent, such as a WhatsApp
front door, is offered ``start_case`` while Keel grants it a type.  The
schema takes only the granted types, and says what each one requires.  Only
a conversation's own session is offered it: never a session of a case, nor a
helper a conversation started.  Each tool call starts one case, for the
person the conversation is with: Keel keys it by the session and the call,
so the same call sent again finds the case it started.  Keel's refusals are
errors the model can act on; an unreachable Keel is called again; any other
answer is an error a call again would not change.  Asking Keel for the
grants holds a wake briefly at most, and once a minute per agent.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

import surogates.harness.loop as loop_module
import surogates.keel.intake as intake_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.keel.client import KeelClient, keel_client, use_keel_client
from surogates.keel.intake import INTAKE_ASK_SECONDS, INTAKE_CACHE_SECONDS, intake_case_types
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.session.acting_principal import ActingPrincipal
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.storage.backend import LocalBackend
from surogates.tenant.context import TenantContext
from tests.test_case_step_isolation import _SessionStore
from tests.test_case_step_tools import _calls, _sandbox, _StepStore
from tests.test_case_tool_gate import MCP_TOOL, _registry
from tests.test_case_tool_gate import _step as _case_step
from tests.test_steer_loop import _final_response

ORG = UUID("00000000-0000-0000-0000-0000000000a1")
AGENT = "intake-agent"
LOAN = {
    "case_type": "loan",
    "name": "Loan application",
    "form": {
        "type": "object",
        "properties": {"amount": {"type": "number"}, "applicant": {"type": "string"}, "purpose": {"type": "string"}},
    },
    "required": ["amount", "applicant"],
}
COMPLAINT = {
    "case_type": "complaint",
    "name": "Customer complaint",
    "form": {"type": "object", "properties": {"summary": {"type": "string"}}},
    "required": [],
}
DATA = {"amount": 182000, "applicant": "Ioana Pop", "purpose": "a flat"}
STARTED = {"case_id": str(uuid4()), "number": "LN-000001"}


class _FakeKeel:
    """Keel's door: the case types it grants, and the answers to case starts, the last for every start after."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.grants: list[dict] = [LOAN]
        self._starts: list[tuple[int, dict]] = [(201, STARTED)]
        self._told = False
        self.down = False

    def answer(self, status: int, body: dict) -> None:
        if not self._told:
            self._starts, self._told = [], True
        self._starts.append((status, body))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "GET":
            return httpx.Response(200, json=self.grants)
        status, body = self._starts.pop(0) if len(self._starts) > 1 else self._starts[0]
        return httpx.Response(status, json=body)

    def starts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def asks(self) -> list[str]:
        return [r.url.path for r in self.requests if r.method == "GET"]


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> SimpleNamespace:
    """The intake cache's clock, which the test moves, and an empty cache."""
    now = SimpleNamespace(seconds=1000.0)
    monkeypatch.setattr(intake_module, "_now", lambda: now.seconds)
    monkeypatch.setattr(intake_module, "_cache", type(intake_module._cache)())
    monkeypatch.setattr(intake_module, "_asking", {})
    return now


@pytest.fixture
def door():
    """Puts Keel's door behind a handler of the test's."""
    previous = keel_client()

    def behind(handler) -> None:
        use_keel_client(KeelClient("http://keel-door.test", "door-key", transport=httpx.MockTransport(handler)))

    yield behind
    use_keel_client(previous)


@pytest.fixture
def fake_keel(door):
    keel = _FakeKeel()
    door(keel.handle)
    return keel


class _Conversation(_StepStore):
    """The session's store, which knows whom the conversation's latest message is from."""

    def __init__(self, session: Session, person: ActingPrincipal, storage: LocalBackend | None = None) -> None:
        super().__init__(session, storage)
        self.person = person

    async def resolve_acting_principal(self, session_id):
        assert session_id == self.row.id
        return self.person


def _front_door(*, user_id: UUID | None = None, service_account_id: UUID | None = None) -> Session:
    """A WhatsApp conversation of the intake agent."""
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=user_id, service_account_id=service_account_id, org_id=ORG, agent_id=AGENT,
        channel="whatsapp", status="active", config={}, created_at=now, updated_at=now,
    )


def _tenant(session: Session, *, agent_account: UUID | None = None) -> TenantContext:
    """The session's tenant: its own principal, or the agent's service account, as a managed channel's is."""
    return TenantContext(
        org_id=session.org_id, user_id=None if agent_account else session.user_id, org_config={},
        user_preferences={}, permissions=frozenset(), asset_root="/tmp/assets",
        service_account_id=agent_account or session.service_account_id,
    )


def _prompt_tools(session: Session, intake: list[dict]) -> set[str]:
    """The tools the worker tells the model about."""
    return _filter_effective_tools(
        tools=set(_registry().tool_names), tenant=_tenant(session), session=session,
        use_api_for_harness_tools=True, whiteboard_enabled=True, programs_enabled=True, intake=intake,
    )


async def _turn(
    monkeypatch, store: _StepStore, intake: list[dict], replies: list, *, tenant: TenantContext | None = None,
) -> list[dict]:
    """One turn of *store*'s session, by an agent Keel grants *intake*: the requests the model was sent."""
    sent: list[dict] = []
    script = iter(replies)

    async def llm(**call):
        sent.append(call["create_kwargs"])
        try:
            return next(script)
        except StopIteration:
            raise AssertionError("the model was called more often than scripted") from None

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    harness = AgentHarness(
        session_store=store, tool_registry=_registry(), llm_client=AsyncMock(), tenant=tenant or _tenant(store.row),
        worker_id="w", budget=IterationBudget(max_total=4),
        context_compressor=SimpleNamespace(context_length=1000, _context_window=200_000, should_compress=lambda *a, **k: False),
        prompt_builder=SimpleNamespace(has_agents=False), sandbox_pool=_sandbox({}),
        mcp_tool_names=frozenset({MCP_TOOL}), intake=intake,
    )
    harness._storage = store.storage
    harness._streaming_enabled = False
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)

    message = "I would like a loan of 182000 for a flat. I am Ioana Pop."
    store.write(EventType.USER_MESSAGE, {"content": message})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": message}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    return sent


def _offered(request: dict) -> dict[str, dict]:
    return {tool["function"]["name"]: tool["function"] for tool in request["tools"]}


async def test_start_case_is_offered_only_with_grants(monkeypatch, tmp_path):
    front_door = _front_door(user_id=uuid4())
    step = await _case_step(_SessionStore())

    for intake in ([], [LOAN]):
        for session in (front_door, step):
            offered = bool(intake) and session is front_door
            assert ("start_case" in _prompt_tools(session, intake)) is offered, (intake, session.channel)
            # A step's turn that ends in words is nudged once to report: its first request is the turn's.
            request, *_ = await _turn(
                monkeypatch, _StepStore(session, LocalBackend(str(tmp_path))), intake, [_final_response("Done.")] * 2,
            )
            assert ("start_case" in _offered(request)) is offered, (intake, session.channel)

    [request] = await _turn(
        monkeypatch, _StepStore(front_door, LocalBackend(str(tmp_path))), [LOAN], [_final_response("Done.")],
    )
    start_case = _offered(request)["start_case"]
    assert start_case["parameters"]["properties"]["case_type"]["enum"] == ["loan"]
    assert "- loan (Loan application): requires amount, applicant" in start_case["description"]

    # Each granted type is named, with what it requires.
    [request] = await _turn(
        monkeypatch, _StepStore(front_door, LocalBackend(str(tmp_path))), [LOAN, COMPLAINT], [_final_response("Done.")],
    )
    start_case = _offered(request)["start_case"]
    assert start_case["parameters"]["properties"]["case_type"]["enum"] == ["loan", "complaint"]
    assert "- loan (Loan application): requires amount, applicant" in start_case["description"]
    assert "- complaint (Customer complaint): requires no fields" in start_case["description"]


async def test_a_helper_of_an_intake_session_is_not_offered_start_case(monkeypatch, tmp_path):
    # A helper the conversation started would start cases of its own, each session up to Keel's limit.
    front_door = _front_door(user_id=uuid4())
    helper = front_door.model_copy(update={"id": uuid4(), "parent_id": front_door.id, "channel": "delegation"})

    assert "start_case" not in _prompt_tools(helper, [LOAN])
    [request] = await _turn(
        monkeypatch, _StepStore(helper, LocalBackend(str(tmp_path))), [LOAN], [_final_response("Done.")],
    )
    assert "start_case" not in _offered(request)
    assert "read_file" in _offered(request)


async def test_start_case_starts_one_case_per_tool_call(fake_keel, monkeypatch, tmp_path):
    # A WhatsApp conversation: the tenant is the agent's own service account, and the person is its latest sender.
    person = uuid4()
    session = _front_door(user_id=person)
    store = _Conversation(session, ActingPrincipal(user_id=person, service_account_id=None), LocalBackend(str(tmp_path)))
    agent_account = uuid4()
    fake_keel.answer(201, STARTED)
    # The same call again finds the case it started; another call is another case.
    fake_keel.answer(200, STARTED)
    fake_keel.answer(201, {"case_id": str(uuid4()), "number": "LN-000002"})
    loan = {"case_type": "loan", "data": DATA}

    await _turn(monkeypatch, store, [LOAN], [
        _calls("start_case", loan, "call-1"),
        _calls("start_case", loan, "call-1"),
        _calls("start_case", loan, "call-2"),
        _final_response("Your application is case LN-000001."),
    ], tenant=_tenant(session, agent_account=agent_account))

    assert [data["content"] for _, kind, data in store.events if kind is EventType.TOOL_RESULT] == [
        "Case LN-000001 started (loan).", "Case LN-000001 started (loan).", "Case LN-000002 started (loan).",
    ]
    session_id = store.row.id
    first, again, second = fake_keel.starts()
    assert first.url.path == f"/api/harness/projects/{ORG}/cases"
    assert first.headers["authorization"] == "Bearer door-key"
    assert [r.headers["idempotency-key"] for r in (first, again, second)] == [
        f"intake:{session_id}:call-1", f"intake:{session_id}:call-1", f"intake:{session_id}:call-2",
    ]
    assert json.loads(first.content) == {
        "agent_id": AGENT, "session_id": str(session_id), "user_id": str(person), "channel": "whatsapp",
        "case_type": "loan", "data": DATA,
    }
    # An intake session is no step: Keel is never told how it ended.
    assert all(r.url.path.endswith("/cases") for r in fake_keel.requests)


async def test_a_conversation_with_no_person_starts_a_case_for_no_user(fake_keel):
    # An API caller's session: its sender is a service account, no person.
    account = uuid4()
    session = _front_door(service_account_id=account)

    result = await _start(session, {"case_type": "loan", "data": DATA})

    assert result == "Case LN-000001 started (loan)."
    [request] = fake_keel.starts()
    assert json.loads(request.content)["user_id"] is None


async def _start(session: Session, arguments: dict, call_id: str = "call-1") -> str:
    """``start_case`` run as the harness runs it for *session*, whose sender is its own principal."""
    person = ActingPrincipal(user_id=session.user_id, service_account_id=session.service_account_id)
    return await _registry().dispatch(
        "start_case", arguments,
        session_id=str(session.id), agent_id=session.agent_id, tenant=_tenant(session),
        session_store=_Conversation(session, person), channel=session.channel, tool_call_id=call_id,
        session_config=session.config,
    )


@pytest.mark.parametrize(("status", "code", "message"), [
    (403, "not-granted", "The case type loan does not list this agent among its intake agents."),
    (409, "intake-limit", "A session starts at most 5 cases."),
    (422, "invalid-request", "body.data.amount: must be positive"),
], ids=["not-granted", "intake-limit", "rejected"])
async def test_refusals_are_tool_errors(fake_keel, status, code, message):
    fake_keel.answer(status, {"code": code, "message": message})

    result = await _start(_front_door(user_id=uuid4()), {"case_type": "loan", "data": DATA})

    assert json.loads(result) == {"error": message, "code": code}


@pytest.mark.parametrize(("answer", "again"), [
    (None, True),
    ((408, {"code": "timeout", "message": "Too slow."}), True),
    ((429, {"code": "busy", "message": "Slow down."}), True),
    ((500, {"code": "internal", "message": "Boom."}), True),
    ((503, {"code": "unavailable", "message": "Starting."}), True),
    ((503, {"code": "harness-off", "message": "The door is closed."}), False),
    ((401, {"code": "unauthenticated", "message": "Wrong key."}), False),
    ((404, {"code": "not-found", "message": "No such route."}), False),
], ids=["unreachable", "408", "429", "500", "503", "harness-off", "401", "404"])
async def test_an_unreachable_keel_is_called_again_and_any_other_answer_is_refused(fake_keel, answer, again):
    if answer is None:
        fake_keel.down = True
    else:
        fake_keel.answer(*answer)

    error = json.loads(await _start(_front_door(user_id=uuid4()), {"case_type": "loan", "data": DATA}))["error"]

    if again:
        assert error == "Keel could not be reached; call start_case again."
    else:
        assert error == f"Keel refused this request ({answer[0]})."
    assert len(fake_keel.starts()) == 1


@pytest.mark.parametrize(("arguments", "call_id", "error"), [
    ({"case_type": "loan", "data": {"notes": "x" * 300_000}}, "call-1", "bytes, over the 256,000 bytes Keel takes."),
    ({"case_type": "loan", "data": {"amount": float("nan")}}, "call-1", "a number JSON cannot carry"),
    ({"case_type": "loan", "data": "182000 for a flat"}, "call-1", "data must be an object"),
    ({"data": DATA}, "call-1", "case_type must name"),
    ({"case_type": "loan", "data": DATA}, "c" * 157, "longer than"),
], ids=["too-large", "nan", "data-not-an-object", "no-case-type", "call-id-too-long"])
async def test_a_start_keel_could_not_take_is_never_sent(fake_keel, arguments, call_id, error):
    result = json.loads(await _start(_front_door(user_id=uuid4()), arguments, call_id))

    assert error in result["error"]
    assert fake_keel.starts() == []


async def test_the_longest_call_id_keel_keys_is_sent(fake_keel):
    session = _front_door(user_id=uuid4())

    assert await _start(session, {"case_type": "loan", "data": DATA}, "c" * 156) == "Case LN-000001 started (loan)."

    [request] = fake_keel.starts()
    assert len(request.headers["idempotency-key"]) == 200


async def test_grants_are_asked_once_a_minute_per_agent(fake_keel, clock):
    assert await intake_case_types(ORG, AGENT) == [LOAN]
    clock.seconds += INTAKE_CACHE_SECONDS - 1
    assert await intake_case_types(ORG, AGENT) == [LOAN]
    assert fake_keel.asks() == [f"/api/harness/projects/{ORG}/agents/{AGENT}/intake"]
    assert fake_keel.requests[0].headers["authorization"] == "Bearer door-key"

    # Another agent is asked for its own.
    fake_keel.grants = []
    assert await intake_case_types(ORG, "other-agent") == []
    assert len(fake_keel.asks()) == 2

    # A minute on, the agent is asked again.
    fake_keel.grants = [LOAN, COMPLAINT]
    clock.seconds += 2
    assert await intake_case_types(ORG, AGENT) == [LOAN, COMPLAINT]
    assert len(fake_keel.asks()) == 3

    # An unreachable Keel grants nothing, for a minute too.
    fake_keel.down = True
    clock.seconds += INTAKE_CACHE_SECONDS + 1
    assert await intake_case_types(ORG, AGENT) == []
    fake_keel.down = False
    assert await intake_case_types(ORG, AGENT) == []
    assert len(fake_keel.requests) == 4
    clock.seconds += INTAKE_CACHE_SECONDS + 1
    assert await intake_case_types(ORG, AGENT) == [LOAN, COMPLAINT]


async def test_a_silent_keel_holds_a_wake_at_most_briefly(door):
    asked: list[httpx.Request] = []

    async def silent(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        await asyncio.Event().wait()
        raise AssertionError("a silent door never answers")

    door(silent)

    started = time.monotonic()
    # Bounded here too, so a wake held without end fails rather than hangs.
    assert await asyncio.wait_for(intake_case_types(ORG, AGENT), INTAKE_ASK_SECONDS + 3) == []
    assert time.monotonic() - started < INTAKE_ASK_SECONDS + 1

    # The silence is kept a minute, as an empty list is: the next wake is not held.
    started = time.monotonic()
    assert await intake_case_types(ORG, AGENT) == []
    assert time.monotonic() - started < 0.5
    assert len(asked) == 1


async def test_wakes_at_the_minute_share_one_ask(door, clock):
    asked: list[httpx.Request] = []
    answer = asyncio.Event()
    answer.set()

    async def slow(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        await answer.wait()
        return httpx.Response(200, json=[LOAN])

    door(slow)
    assert await intake_case_types(ORG, AGENT) == [LOAN]

    answer.clear()
    clock.seconds += INTAKE_CACHE_SECONDS + 1
    wakes = [asyncio.create_task(intake_case_types(ORG, AGENT)) for _ in range(5)]
    while len(asked) < 2:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    answer.set()

    assert await asyncio.wait_for(asyncio.gather(*wakes), 5) == [[LOAN]] * 5
    assert len(asked) == 2


async def test_a_runtime_without_keel_grants_nothing():
    previous = keel_client()
    use_keel_client(None)
    try:
        assert await intake_case_types(ORG, AGENT) == []
    finally:
        use_keel_client(previous)
