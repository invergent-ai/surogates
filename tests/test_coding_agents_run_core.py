"""Unit tests for the shared coding-run core (fakes for store/sandbox)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CodingAgentCredentials, CredentialBundle
from surogates.coding_agents.run_core import CodingRunOutcome, execute_coding_run
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStore:
    def __init__(self):
        self.events = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)


class _FakeVault:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    async def retrieve(self, org_id, name, user_id=None):
        return self.stored.get(name)

    async def store(self, org_id, name, value, user_id=None):
        self.stored[name] = value
        return (uuid4(), True)

    async def delete(self, org_id, name, user_id=None):
        return self.stored.pop(name, None) is not None


def _sbx(polls):
    calls = []

    async def execute(name, input_json):
        payload = json.loads(input_json)
        calls.append((payload["action"], payload))
        if payload["action"] == "launch":
            return json.dumps({"ok": True, "run_id": payload["run_id"], "pid": 1})
        if payload["action"] == "poll":
            return json.dumps(polls.pop(0))
        return json.dumps({"ok": True})

    return execute, calls


def _tenant():
    return SimpleNamespace(org_id=uuid4(), user_id=uuid4())


async def _noop_ensure():
    return None


async def test_not_connected_returns_status():
    store = _FakeStore()
    creds = CodingAgentCredentials(_FakeVault())
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=creds, agent="claude", provider="anthropic",
        prompt="do it", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=lambda n, i: None,
        should_cancel=lambda: False,
    )
    assert isinstance(outcome, CodingRunOutcome)
    assert outcome.status == "not_connected"
    assert outcome.result is None
    # No CODE_RUN_STARTED emitted when not connected.
    assert all(et != EventType.CODE_RUN_STARTED for et, _ in store.events)


async def test_run_emits_events_and_returns_result():
    store = _FakeStore()
    creds = CodingAgentCredentials(_FakeVault({
        "code_cred:anthropic": CredentialBundle(
            provider="anthropic", auth_mode="oauth",
            token_kind="setup_token", oauth_token="sk-ant-oat01-x",
        ).to_json(),
    }))
    polls = [{
        "ok": True, "done": True, "exit_code": 0, "offset": 40,
        "new_output": json.dumps({"type": "result", "result": "Built it.",
                                  "usage": {"input_tokens": 5, "output_tokens": 2}}) + "\n",
    }]
    execute, calls = _sbx(polls)
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=creds, agent="claude", provider="anthropic",
        prompt="build a thing", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
        started_metadata={"source_event_id": 42},
    )
    assert outcome.status == "ok"
    assert outcome.result.final_message == "Built it."
    assert outcome.result_event_id is not None
    types = [et for et, _ in store.events]
    assert EventType.CODE_RUN_STARTED in types
    assert EventType.CODE_RUN_RESULT in types
    # started_metadata is merged into the STARTED payload (slash idempotency).
    started = next(d for et, d in store.events if et == EventType.CODE_RUN_STARTED)
    assert started["source_event_id"] == 42
    # Credential reached the launch env, never an event payload.
    launch = next(p for a, p in calls if a == "launch")
    assert launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    for _et, data in store.events:
        assert "sk-ant-oat01-x" not in json.dumps(data)


async def test_codex_writeback_surfaced():
    store = _FakeStore()
    creds = CodingAgentCredentials(_FakeVault({
        "code_cred:openai": CredentialBundle(
            provider="openai", auth_mode="oauth",
            auth_json={"tokens": {"access_token": "old"}},
        ).to_json(),
    }))
    polls = [{
        "ok": True, "done": True, "exit_code": 0, "offset": 20,
        "new_output": json.dumps({"type": "item.completed",
                                  "item": {"type": "agent_message", "text": "ok"}}) + "\n",
        "codex_auth_json": json.dumps({"tokens": {"access_token": "fresh"}}),
    }]
    execute, _calls = _sbx(polls)
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=creds, agent="codex", provider="openai",
        prompt="review", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
    )
    assert outcome.status == "ok"
    # Refreshed codex auth re-stored into the vault.
    stored = json.loads(creds._vault.stored["code_cred:openai"])
    assert stored["auth_json"]["tokens"]["access_token"] == "fresh"
