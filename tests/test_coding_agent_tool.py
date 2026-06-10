"""Unit tests for the run_coding_agent tool handler (fake kwargs)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CredentialBundle
from surogates.tools.builtin.coding_agent import _run_coding_agent_handler
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStore:
    def __init__(self, session):
        self._session = session
        self.events = []

    async def get_session(self, sid):
        return self._session

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)


class _FakeVault:
    def __init__(self, stored):
        self.stored = dict(stored)

    async def retrieve(self, org_id, name, user_id=None):
        return self.stored.get(name)

    async def store(self, org_id, name, value, user_id=None):
        self.stored[name] = value
        return (uuid4(), True)


def _sandbox(polls):
    async def execute(owner, name, input_json):
        payload = json.loads(input_json)
        if payload["action"] == "launch":
            return json.dumps({"ok": True, "run_id": payload["run_id"], "pid": 1})
        if payload["action"] == "poll":
            return json.dumps(polls.pop(0))
        return json.dumps({"ok": True})

    async def ensure(owner, spec):
        return None

    return SimpleNamespace(execute=execute, ensure=ensure)


async def _aw_none():
    return None


async def test_handler_runs_and_returns_final_message(monkeypatch):
    session = SimpleNamespace(id=uuid4(), config={}, agent_id="a")
    store = _FakeStore(session)
    tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    vault = _FakeVault({
        "code_cred:anthropic": CredentialBundle(
            provider="anthropic", auth_mode="api_key", api_key="sk-ant-api03-x",
        ).to_json(),
    })
    polls = [{
        "ok": True, "done": True, "exit_code": 0, "offset": 30,
        "new_output": json.dumps({"type": "result", "result": "Implemented.",
                                  "usage": {"input_tokens": 9, "output_tokens": 4}}) + "\n",
    }]
    # Bypass the real SandboxSpec builder.
    import surogates.tools.builtin.coding_agent as mod
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)

    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "implement the feature"},
        tenant=tenant, session_id=str(session.id), session_store=store,
        sandbox_pool=_sandbox(polls), credential_vault=vault,
    )
    data = json.loads(out)
    assert data["final_message"] == "Implemented."
    assert data["input_tokens"] == 9
    types = [et for et, _ in store.events]
    assert EventType.CODE_RUN_RESULT in types


async def test_handler_rejects_bad_agent():
    out = await _run_coding_agent_handler(
        {"agent": "gemini", "prompt": "x"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4()),
        session_id=str(uuid4()), session_store=None, sandbox_pool=None,
        credential_vault=None,
    )
    assert "error" in json.loads(out)


async def test_handler_not_connected_returns_error():
    session = SimpleNamespace(id=uuid4(), config={}, agent_id="a")
    store = _FakeStore(session)
    out = await _run_coding_agent_handler(
        {"agent": "codex", "prompt": "review"},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4()),
        session_id=str(session.id), session_store=store,
        sandbox_pool=_sandbox([]), credential_vault=_FakeVault({}),
    )
    data = json.loads(out)
    assert "not connected" in data["error"].lower()


async def test_handler_requires_prompt():
    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "  "},
        tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4()),
        session_id=str(uuid4()), session_store=None, sandbox_pool=None,
        credential_vault=None,
    )
    assert "prompt" in json.loads(out)["error"].lower()
