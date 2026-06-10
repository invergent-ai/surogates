"""Unit tests for CodeCommandMixin via a fake harness (no real AgentHarness)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CredentialBundle
from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)  # real store returns a BIGSERIAL int id

    async def advance_harness_cursor(self, session_id, *, through_event_id, lease_token):
        return None


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


class _FakeSandboxPool:
    """Scripted pod: launch ok, then the given poll responses in order."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.calls: list[tuple] = []
        self.ensured = False

    async def ensure(self, owner, spec):
        self.ensured = True

    async def execute(self, owner, name, input_json):
        payload = json.loads(input_json)
        action = payload["action"]
        self.calls.append((action, payload))
        if action == "launch":
            return json.dumps({"ok": True, "run_id": payload["run_id"], "pid": 7})
        if action == "poll":
            return json.dumps(self.polls.pop(0))
        return json.dumps({"ok": True})


class _Harness(CodeCommandMixin):
    def __init__(self, vault=None, sandbox_pool=None):
        self._store = _FakeStore()
        self._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
        self._credential_vault = vault
        self._sandbox_pool = sandbox_pool
        self._interrupt_requested = False

    async def _ensure_code_sandbox(self, session, sandbox_owner):
        # Bypass the real SandboxSpec builder in unit tests.
        await self._sandbox_pool.ensure(sandbox_owner, None)


def _session():
    return SimpleNamespace(id=uuid4(), config={})


def _lease():
    return SimpleNamespace(lease_token="lease-token")


def _last_message(harness) -> str:
    event_type, data = harness._store.events[-1]
    assert event_type == EventType.LLM_RESPONSE
    return data["message"]["content"]


async def test_help_emits_usage():
    h = _Harness()
    await h._handle_code_command(_session(), "/code", _lease())
    assert "/code claude" in _last_message(h)


async def test_login_emits_instructions():
    h = _Harness()
    await h._handle_code_command(_session(), "/code login claude", _lease())
    assert "claude setup-token" in _last_message(h)


async def test_status_without_vault_explains():
    h = _Harness(vault=None)
    await h._handle_code_command(_session(), "/code status", _lease())
    assert "vault" in _last_message(h).lower()


def _event_types(harness):
    return [et for et, _ in harness._store.events]


def _bundle_json(**kw):
    return CredentialBundle(**kw).to_json()


async def test_run_without_connection_prompts_connect_first():
    h = _Harness(vault=_FakeVault(), sandbox_pool=_FakeSandboxPool([]))
    await h._handle_code_command(_session(), '/code claude "do it"', _lease())
    assert "/code login claude" in _last_message(h)


async def test_run_without_vault_explains():
    h = _Harness(vault=None, sandbox_pool=_FakeSandboxPool([]))
    await h._handle_code_command(_session(), '/code claude "do it"', _lease())
    assert "vault" in _last_message(h).lower()


async def test_run_streams_and_emits_result():
    vault = _FakeVault({
        "code_cred:anthropic": _bundle_json(
            provider="anthropic", auth_mode="oauth",
            token_kind="setup_token", oauth_token="sk-ant-oat01-x",
        ),
    })
    polls = [
        {"ok": True, "done": False, "exit_code": None, "offset": 10,
         "new_output": json.dumps({"type": "assistant",
                                   "message": {"content": [{"type": "text", "text": "step"}]}}) + "\n"},
        {"ok": True, "done": True, "exit_code": 0, "offset": 60,
         "new_output": json.dumps({"type": "result", "result": "Finished.",
                                   "usage": {"input_tokens": 7, "output_tokens": 3}}) + "\n"},
    ]
    pool = _FakeSandboxPool(polls)
    h = _Harness(vault=vault, sandbox_pool=pool)

    await h._handle_code_command(_session(), '/code claude "do it"', _lease())

    types = _event_types(h)
    assert EventType.CODE_RUN_STARTED in types
    assert EventType.CODE_RUN_PROGRESS in types
    assert EventType.CODE_RUN_RESULT in types

    # The credential reached the launch env, never an event payload.
    launch = next(p for a, p in pool.calls if a == "launch")
    assert launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-x"
    for _et, data in h._store.events:
        assert "sk-ant-oat01-x" not in json.dumps(data)

    result_data = next(d for et, d in h._store.events if et == EventType.CODE_RUN_RESULT)
    assert result_data["final_message"] == "Finished."
    assert result_data["error"] is None
    assert pool.ensured is True


async def test_run_idempotent_skips_relaunch():
    vault = _FakeVault({
        "code_cred:openai": _bundle_json(
            provider="openai", auth_mode="api_key", api_key="sk-proj-x",
        ),
    })
    pool = _FakeSandboxPool([])
    h = _Harness(vault=vault, sandbox_pool=pool)

    user_evt = SimpleNamespace(type=EventType.USER_MESSAGE.value, data={}, id=5)
    started = SimpleNamespace(
        type=EventType.CODE_RUN_STARTED.value,
        data={"source_event_id": 5}, id=6,
    )
    all_events = [user_evt, started]

    await h._handle_code_command(_session(), '/code codex "again"', _lease(), all_events)
    # Already started for this source event — no relaunch, no new events.
    assert pool.calls == []
    assert h._store.events == []


async def test_run_interrupt_cancels():
    vault = _FakeVault({
        "code_cred:anthropic": _bundle_json(
            provider="anthropic", auth_mode="api_key", api_key="sk-ant-api03-x",
        ),
    })
    polls = [
        {"ok": True, "done": False, "exit_code": None, "offset": 3, "new_output": "..."},
    ]
    pool = _FakeSandboxPool(polls)
    h = _Harness(vault=vault, sandbox_pool=pool)
    h._interrupt_requested = True  # interrupt before the first poll

    await h._handle_code_command(_session(), '/code claude "stop"', _lease())

    assert any(a == "cancel" for a, _ in pool.calls)
    result_data = next(d for et, d in h._store.events if et == EventType.CODE_RUN_RESULT)
    assert "interrupt" in (result_data["error"] or "").lower()
