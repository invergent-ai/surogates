"""A consulted expert's model is sent only tools its session could be sent.

``consult_expert`` (and ``/<expert>``) run the expert's own loop with the
tools its ``expert_tools`` name.  The session's request went through
``drop_unusable_tools`` and its policy; the expert's did not, so an expert
was sent an unwired ``generate_image``, a knowledge-base tool with no
knowledge base, a tool the policy denies or one that needs approval, and its
calls ran under an open gate.  An expert sent no tools at all could still have
any call it made run.
"""

from __future__ import annotations

import contextvars
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from agent_os.egress_policy import EgressPolicy

import surogates.harness.loop as loop_module
from surogates.governance.policy import GovernanceGate
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.harness.tool_schemas import drop_unusable_tools, unusable_tool_names
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.storage.backend import LocalBackend
from surogates.tenant.context import TenantContext
from surogates.tools.builtin import expert_loop as el
from surogates.tools.builtin.media_gen import MediaGenConfig
from surogates.tools.loader import SkillDef
from tests.test_case_step_tools import _sandbox, _StepStore
from tests.test_case_tool_gate import MCP_TOOL, _registry
from tests.test_steer_loop import _final_response

EXPERT_TOOLS = ["read_file", "terminal", "write_file", "generate_image", "kb_search_pages"]


def _expert(tools: list[str] | None = None) -> SkillDef:
    return SkillDef(
        name="drafter", description="Drafts", content="Draft well.", source="org", type="expert",
        expert_model="m", expert_endpoint="http://expert.test/v1", expert_tools=tools,
        expert_max_iterations=3, expert_status="active",
    )


class _ExpertModel:
    """The expert's endpoint: asks for *calls* once, then answers; keeps what each request was sent."""

    def __init__(self, calls: list[str], arguments: str = "{}") -> None:
        self.calls = calls
        self.arguments = arguments
        self.requests: list[dict] = []

    def client(self, **_kwargs):
        model = self

        class _Completions:
            async def create(self, **kwargs):
                model.requests.append(kwargs)
                if len(model.requests) == 1 and model.calls:
                    calls = [
                        SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=name, arguments=model.arguments))
                        for i, name in enumerate(model.calls)
                    ]
                    message = SimpleNamespace(content=None, tool_calls=calls)
                else:
                    message = SimpleNamespace(content="Drafted.", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        async def close():
            return None

        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()), close=close)

    def sent(self) -> set[str]:
        return {tool["function"]["name"] for tool in self.requests[0].get("tools", [])}

    def results(self) -> dict[str, str]:
        return {m["tool_call_id"]: m["content"] for m in self.requests[-1]["messages"] if m["role"] == "tool"}


def test_unusable_tool_names_is_what_the_session_filter_drops():
    schemas = _registry().get_schemas()
    names = {s["function"]["name"] for s in schemas}
    for flags in (
        dict(has_kbs=False, has_channel=False, makes_routines=False, has_image_model=False, has_video_model=False),
        dict(has_kbs=True, has_channel=True, makes_routines=True, is_voice=True, has_file_pane=False),
        dict(has_kbs=True, has_channel=False, makes_routines=True, confirms_routines=False, reads_routines=False),
    ):
        kept = {s["function"]["name"] for s in drop_unusable_tools(schemas, **flags)}
        assert kept == names - unusable_tool_names(**flags), flags


def test_runs_unasked_is_offers_less_the_tools_that_need_approval():
    gate = GovernanceGate(denied_tools={"terminal"}, require_approval={"write_file"})
    assert not gate.runs_unasked("terminal") and not gate.offers("terminal")
    assert not gate.runs_unasked("write_file") and gate.offers("write_file")
    assert gate.runs_unasked("read_file") and gate.runs_unasked(MCP_TOOL)
    allow_list = GovernanceGate(allowed_tools={"read_file"})
    assert allow_list.runs_unasked("read_file") and not allow_list.runs_unasked("terminal")
    assert GovernanceGate(denied_tools={"terminal"}, enabled=False).runs_unasked("terminal")


async def test_the_expert_is_sent_only_what_its_scope_leaves_and_calls_only_that(monkeypatch):
    model = _ExpertModel(["generate_image", "read_file"])
    monkeypatch.setattr("openai.AsyncOpenAI", model.client)
    registry = _registry()
    router = SimpleNamespace(execute=AsyncMock(return_value="contents"))
    token = el.expert_scope.set(el.ExpertScope(withheld=frozenset({"generate_image", "terminal"})))
    try:
        result, _ = await el.run_expert_loop(
            expert=_expert(EXPERT_TOOLS), task="t", context=None, tool_router=router, tool_registry=registry,
            tenant=SimpleNamespace(org_config={}), session_id=uuid4(),
        )
    finally:
        el.expert_scope.reset(token)
    assert result == "Drafted."
    assert model.sent() == {"read_file", "write_file", "kb_search_pages"}
    system = model.requests[0]["messages"][0]["content"]
    assert "these tools: read_file, write_file, kb_search_pages." in system
    # The withheld tool it asked for anyway is refused; only what it was sent runs.
    assert [c.kwargs["name"] for c in router.execute.await_args_list] == ["read_file"]
    assert "not available to this expert" in json.loads(model.results()["c0"])["error"]


async def test_an_expert_sent_no_tools_runs_none(monkeypatch):
    model = _ExpertModel(["terminal"])
    monkeypatch.setattr("openai.AsyncOpenAI", model.client)
    router = SimpleNamespace(execute=AsyncMock(return_value="ran"))
    await el.run_expert_loop(
        expert=_expert(None), task="t", context=None, tool_router=router, tool_registry=_registry(),
        tenant=SimpleNamespace(org_config={}), session_id=uuid4(),
    )
    assert "tools" not in model.requests[0]
    router.execute.assert_not_awaited()
    assert json.loads(model.results()["c0"])["error"].endswith("It has no tools.")


def test_without_a_scope_nothing_is_withheld():
    assert el.expert_scope.get() == el.ExpertScope()


def _session(channel: str = "web", config: dict | None = None) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel=channel, status="active",
        config=config or {}, created_at=now, updated_at=now,
    )


def _harness(store, *, media_gen, gate, prompt=None) -> AgentHarness:
    tenant = TenantContext(
        org_id=store.row.org_id, user_id=store.row.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/assets",
    )
    harness = AgentHarness(
        session_store=store, tool_registry=_registry(), llm_client=AsyncMock(), tenant=tenant,
        worker_id="w", budget=IterationBudget(max_total=4),
        context_compressor=SimpleNamespace(context_length=1000, _context_window=200_000, should_compress=lambda *a, **k: False),
        prompt_builder=prompt or SimpleNamespace(has_agents=False, has_kbs=False), sandbox_pool=_sandbox({}),
        mcp_tool_names=frozenset({MCP_TOOL}), media_gen=media_gen, governance_gate=gate,
    )
    harness._storage = store.storage
    harness._streaming_enabled = False
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    return harness


async def _consult_in_a_turn(
    monkeypatch, tmp_path, *, media_gen, gate, calls, config: dict | None = None, arguments: str = "{}",
    expert_tools: list[str] = EXPERT_TOOLS,
) -> tuple[_ExpertModel, MagicMock]:
    """A real ``_run_loop`` turn whose model consults the expert once; the expert asks for *calls*."""
    model = _ExpertModel(calls, arguments)
    monkeypatch.setattr("openai.AsyncOpenAI", model.client)
    monkeypatch.setattr(
        "surogates.tools.builtin.expert._resolve_expert_local",
        AsyncMock(return_value=(_expert(expert_tools), ["drafter"])),
    )
    answers = iter([
        ({"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "consult_expert", "arguments": json.dumps({"expert": "drafter", "task": "Draft it."})}}]},
         {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1}),
        _final_response("Done."),
    ])

    async def llm(**_call):
        return next(answers)

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    session = _session(config=config)
    store = _StepStore(session, LocalBackend(str(tmp_path)))
    harness = _harness(store, media_gen=media_gen, gate=gate)
    store.write(EventType.USER_MESSAGE, {"content": "Draft it."})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": "Draft it."}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    return model, harness._sandbox_pool


async def test_an_expert_consulted_in_a_turn_gets_only_what_the_session_could_be_sent(monkeypatch, tmp_path):
    gate = GovernanceGate(denied_tools={"terminal"}, require_approval={"write_file"})
    model, sandbox = await _consult_in_a_turn(
        monkeypatch, tmp_path, media_gen=MediaGenConfig(), gate=gate, calls=["terminal", "write_file"],
    )
    # No image model, no knowledge base, a denied tool and one that needs approval: none is sent.
    assert model.sent() == {"read_file"}
    # It asked for the denied tool and the one that needs approval anyway: neither ran.
    executed = [c.args[1] for c in sandbox.execute.await_args_list]
    assert "terminal" not in executed and "write_file" not in executed
    assert all("not available to this expert" in r for r in model.results().values())


async def test_an_expert_keeps_a_tool_the_session_could_be_sent(monkeypatch, tmp_path):
    image = MediaGenConfig(image_client=object(), image_model="image-1")
    model, _ = await _consult_in_a_turn(monkeypatch, tmp_path, media_gen=image, gate=GovernanceGate(), calls=[])
    assert model.sent() == {"read_file", "terminal", "write_file", "generate_image"}


async def test_an_expert_gets_no_tool_the_sessions_filter_takes_away(monkeypatch, tmp_path):
    """An agent that names its tools: its expert works within them too."""
    model, sandbox = await _consult_in_a_turn(
        monkeypatch, tmp_path, media_gen=MediaGenConfig(image_client=object(), image_model="image-1"),
        gate=GovernanceGate(), calls=["terminal"], config={"allowed_tools": ["consult_expert", "read_file"]},
    )
    assert model.sent() == {"read_file"}
    assert "terminal" not in [c.args[1] for c in sandbox.execute.await_args_list]


async def test_the_sessions_policy_checks_an_experts_call_on_its_arguments(monkeypatch, tmp_path):
    """Sent by name, refused on its arguments: the expert's router holds the session's gate, egress rules included."""
    gate = GovernanceGate(egress_policy=EgressPolicy(default_action="deny"))
    model, _ = await _consult_in_a_turn(
        monkeypatch, tmp_path, media_gen=MediaGenConfig(), gate=gate, calls=["web_extract"],
        arguments=json.dumps({"url": "https://exfil.test/x"}), expert_tools=["web_extract"],
    )
    assert "web_extract" in model.sent()
    refused = json.loads(model.results()["c0"])
    assert refused["error"] == "policy_denied" and "Egress policy violation" in refused["reason"]


async def test_the_session_policy_checks_the_experts_calls(monkeypatch, tmp_path):
    """The expert's router is built with the session's gate (an open one only where none is known)."""
    from surogates.tools.builtin.expert_service import ExpertConsultationService

    gate = GovernanceGate(denied_tools={"terminal"})
    pool = _sandbox({})
    service = ExpertConsultationService(
        tenant=SimpleNamespace(org_config={}), session_id=uuid4(), tool_registry=_registry(), sandbox_pool=pool,
    )
    token = el.expert_scope.set(el.ExpertScope(gate=gate))
    try:
        router = service._resolve_tool_router()
    finally:
        el.expert_scope.reset(token)
    assert router.governance is gate
    refused = json.loads(await router.execute(name="terminal", arguments="{}", tenant=None, session_id=uuid4()))
    assert refused["error"] == "policy_denied"
    pool.execute.assert_not_awaited()
    # Where no gate is known, an open one, as before.
    assert service._resolve_tool_router().governance is not gate


async def test_a_slash_expert_is_scoped_before_it_runs(monkeypatch):
    """``/<expert> task`` runs the expert in ``wake``, before the turn's tool list exists: it is scoped first."""
    from tests.test_wake_slash_command_gate import _harness as _wake_harness, _permissive, _stub_store, _user_event

    seen: list[el.ExpertScope] = []

    class _Seen(Exception):
        pass

    async def expand(**_kwargs):
        seen.append(el.expert_scope.get())
        raise _Seen  # the wake stops here: only the scope the expert would run under matters

    monkeypatch.setattr(loop_module, "expand_slash_skill", expand)
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    session = _session(config={"allowed_tools": ["consult_expert", "read_file", "terminal", "generate_image"]})
    gate = GovernanceGate(denied_tools={"terminal"})
    harness = _wake_harness(_stub_store(session, [_user_event(10, "/drafter Draft it.")]), _permissive())
    harness._tools = _registry()
    harness._governance_gate = gate
    harness._media_gen = MediaGenConfig()
    harness._prompt.has_kbs = False
    with pytest.raises(_Seen):
        await harness.wake(session.id)
    assert seen, "the slash expansion was not reached"
    # Unwired, denied, no knowledge base, and outside the agent's own tools.
    assert {"generate_image", "terminal", "kb_search_pages", "write_file"} <= seen[0].withheld
    assert "read_file" not in seen[0].withheld
    assert seen[0].gate is gate


@pytest.mark.parametrize("channel", ["web", "api"])
def test_the_session_and_its_experts_are_cut_by_the_same_facts(channel, tmp_path):
    session = _session(channel)
    store = _StepStore(session, LocalBackend(str(tmp_path)))
    harness = _harness(store, media_gen=MediaGenConfig(), gate=None)
    usable = harness._tool_usability(session, harness._tools.tool_names)

    def scoped() -> el.ExpertScope:
        harness._withhold_from_experts(usable, None)
        return el.expert_scope.get()

    scope = contextvars.copy_context().run(scoped)
    names = set(harness._tools.tool_names)
    kept = {s["function"]["name"] for s in drop_unusable_tools(harness._tools.get_schemas(), **usable)}
    assert scope.withheld == unusable_tool_names(**usable)
    assert kept == names - scope.withheld
