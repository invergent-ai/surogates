"""Unit tests for the run_coding_agent tool handler (fake kwargs).

The tool runs a coding agent on a *configured repo* and opens a PR: it requires
a repo in ``session.config['repos']`` and a git PAT, and returns the PR URL when
the coding CLI opens one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CredentialBundle
from surogates.session.events import EventType
from surogates.tools.builtin.coding_agent import _run_coding_agent_handler

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}


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

    async def retrieve(self, org_id, name, user_id=None, service_account_id=None):
        return self.stored.get(name)

    async def store(self, org_id, name, value, user_id=None, service_account_id=None):
        self.stored[name] = value
        return (uuid4(), True)

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        return self.stored.get(ref.split("://", 1)[1])


def _sandbox(polls):
    calls = []

    async def execute(owner, name, input_json):
        payload = json.loads(input_json)
        calls.append(payload["action"])
        if payload["action"] == "launch":
            return json.dumps({"ok": True, "run_id": payload["run_id"], "pid": 1})
        if payload["action"] == "poll":
            return json.dumps(polls.pop(0))
        return json.dumps({"ok": True})

    async def ensure(owner, spec):
        return None

    return SimpleNamespace(execute=execute, ensure=ensure, _calls=calls)


async def _aw_none():
    return None


def _session(repos=(_REPO,)):
    return SimpleNamespace(
        id=uuid4(), config={"repos": [dict(r) for r in repos]}, agent_id="a",
    )


def _tenant():
    return SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None)


def _anthropic_vault(**extra):
    return _FakeVault({
        "code_cred:anthropic": CredentialBundle(
            provider="anthropic", auth_mode="api_key", api_key="sk-ant-api03-x",
        ).to_json(),
        **extra,
    })


def _result_poll(text):
    return [{
        "ok": True, "done": True, "exit_code": 0, "offset": 30,
        "new_output": json.dumps({
            "type": "result", "result": text,
            "usage": {"input_tokens": 9, "output_tokens": 4},
        }) + "\n",
    }]


async def test_handler_runs_repo_and_returns_pr_url(monkeypatch):
    session = _session()
    store = _FakeStore(session)
    vault = _anthropic_vault(git_pat="github_pat_abc")
    sandbox = _sandbox(_result_poll("Opened https://github.com/acme/api/pull/42"))

    import surogates.tools.builtin.coding_agent as mod
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)

    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "implement the feature"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=sandbox, credential_vault=vault,
    )
    data = json.loads(out)
    assert data["final_message"].startswith("Opened")
    assert data["pr_url"] == "https://github.com/acme/api/pull/42"
    assert data["repo"] == _REPO["url"]
    assert data["branch"].startswith("fix/")
    assert data["checkout_dir"] == "/workspace/api"
    # The repo was checked out before the CLI launched.
    assert sandbox._calls[0] == "checkout"
    assert EventType.CODE_RUN_RESULT in [et for et, _ in store.events]


async def test_handler_notes_when_no_pr_url(monkeypatch):
    session = _session()
    store = _FakeStore(session)
    vault = _anthropic_vault(git_pat="github_pat_abc")
    sandbox = _sandbox(_result_poll("I made the change but could not push."))

    import surogates.tools.builtin.coding_agent as mod
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)

    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "do it"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=sandbox, credential_vault=vault,
    )
    data = json.loads(out)
    assert data["pr_url"] is None
    assert "note" in data


async def test_handler_repo_not_configured_guidance():
    session = _session(repos=())
    store = _FakeStore(session)
    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "do it"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=_sandbox([]), credential_vault=_anthropic_vault(),
    )
    data = json.loads(out)
    assert data["code"] == "repo_not_configured"


async def test_handler_git_pat_not_connected_guidance():
    session = _session()
    store = _FakeStore(session)
    # Coding creds present, but no git PAT.
    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "do it"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=_sandbox([]), credential_vault=_anthropic_vault(),
    )
    data = json.loads(out)
    assert data["code"] == "git_pat_not_connected"


async def test_handler_selects_requested_repo(monkeypatch):
    repos = (
        {"url": "https://github.com/acme/api", "default_branch": "main"},
        {"url": "https://github.com/acme/web", "default_branch": "trunk"},
    )
    session = _session(repos=repos)
    store = _FakeStore(session)
    vault = _anthropic_vault(git_pat="github_pat_abc")
    sandbox = _sandbox(_result_poll("https://github.com/acme/web/pull/7"))

    import surogates.tools.builtin.coding_agent as mod
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)

    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "do it", "repo": "web"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=sandbox, credential_vault=vault,
    )
    data = json.loads(out)
    assert data["repo"] == "https://github.com/acme/web"
    assert data["checkout_dir"] == "/workspace/web"


async def test_handler_not_connected_returns_error(monkeypatch):
    # Repo + PAT configured, but no coding-agent plan connected.
    session = _session()
    store = _FakeStore(session)
    vault = _FakeVault({"git_pat": "github_pat_abc"})
    import surogates.tools.builtin.coding_agent as mod
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)
    out = await _run_coding_agent_handler(
        {"agent": "codex", "prompt": "review"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=_sandbox([]), credential_vault=vault,
    )
    data = json.loads(out)
    assert "not connected" in data["error"].lower()


async def test_handler_rejects_bad_agent():
    out = await _run_coding_agent_handler(
        {"agent": "gemini", "prompt": "x"},
        tenant=_tenant(), session_id=str(uuid4()), session_store=None,
        sandbox_pool=None, credential_vault=None,
    )
    assert "error" in json.loads(out)


async def test_handler_requires_prompt():
    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "  "},
        tenant=_tenant(), session_id=str(uuid4()), session_store=None,
        sandbox_pool=None, credential_vault=None,
    )
    assert "prompt" in json.loads(out)["error"].lower()


async def test_handler_cancels_on_interrupt(monkeypatch):
    import surogates.tools.builtin.coding_agent as mod

    monkeypatch.setattr(mod, "is_interrupted", lambda: True)
    session = _session()
    store = _FakeStore(session)
    vault = _anthropic_vault(git_pat="github_pat_abc")
    calls = []

    async def execute(owner, name, input_json):
        action = json.loads(input_json)["action"]
        calls.append(action)
        if action == "launch":
            return json.dumps({"ok": True, "run_id": "r", "pid": 1})
        return json.dumps({"ok": True})

    async def ensure(owner, spec):
        return None

    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: _aw_none)

    out = await _run_coding_agent_handler(
        {"agent": "claude", "prompt": "long task"},
        tenant=_tenant(), session_id=str(session.id), session_store=store,
        sandbox_pool=SimpleNamespace(execute=execute, ensure=ensure),
        credential_vault=vault,
    )
    # Checkout ran, then the run was cancelled and surfaced as an error.
    assert "cancel" in calls
    assert json.loads(out)["error"]
