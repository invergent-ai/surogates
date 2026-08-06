"""``/code`` on a configured repo: checkout + git auth + PR workflow.

Mirrors ``test_code_command_mixin`` but drives the repo path: a session whose
config carries ``repos`` and a vault holding ``git_pat`` makes ``/code`` clone
the repo on a fresh branch, inject GH_TOKEN, and augment the prompt with the
commit/push/PR instructions.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CredentialBundle
from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PAT = "github_pat_abc0123456789"


class _FakeStore:
    def __init__(self):
        self.events = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)

    async def advance_harness_cursor(self, session_id, *, through_event_id, lease_token):
        return None


class _FakeVault:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    async def retrieve(self, org_id, name, user_id=None, service_account_id=None):
        return self.stored.get(name)

    async def store(self, org_id, name, value, user_id=None, service_account_id=None):
        self.stored[name] = value
        return (uuid4(), True)

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        return self.stored.get(ref.split("://", 1)[1])


class _FakeSandboxPool:
    def __init__(self, polls):
        self.polls = list(polls)
        self.calls = []

    async def ensure(self, owner, spec):
        return None

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
        self._tenant = SimpleNamespace(
            org_id=uuid4(), user_id=uuid4(), service_account_id=None,
        )
        self._credential_vault = vault
        self._sandbox_pool = sandbox_pool
        self._interrupt_requested = False
        self._summary_client = None
        self._summary_model = ""

    async def _ensure_code_sandbox(self, session, sandbox_owner):
        await self._sandbox_pool.ensure(sandbox_owner, None)


def _session(repos=()):
    return SimpleNamespace(
        id=uuid4(), config={"repos": [dict(r) for r in repos]},
    )


def _lease():
    return SimpleNamespace(lease_token="lease-token")


def _vault(**extra):
    return _FakeVault({
        "code_cred:anthropic": CredentialBundle(
            provider="anthropic", auth_mode="oauth",
            token_kind="setup_token", oauth_token="sk-ant-oat01-x",
        ).to_json(),
        **extra,
    })


def _done_polls():
    return [{
        "ok": True, "done": True, "exit_code": 0, "offset": 60,
        "new_output": json.dumps({
            "type": "result", "result": "Opened https://github.com/acme/api/pull/9",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }) + "\n",
    }]


def _last_message(harness):
    et, data = harness._store.events[-1]
    assert et == EventType.LLM_RESPONSE
    return data["message"]["content"]


async def test_code_repo_checks_out_and_augments():
    repo = {"url": "https://github.com/acme/api", "default_branch": "main"}
    pool = _FakeSandboxPool(_done_polls())
    h = _Harness(vault=_vault(git_pat=_PAT), sandbox_pool=pool)

    await h._handle_code_command(_session([repo]), '/code claude "fix the bug"', _lease())

    # Checkout ran before launch, with the token only in the private env.
    checkout = next(p for a, p in pool.calls if a == "checkout")
    assert "$GH_TOKEN" in checkout["command"]
    assert _PAT not in checkout["command"]
    assert checkout["env"]["GH_TOKEN"] == _PAT

    launch = next(p for a, p in pool.calls if a == "launch")
    assert launch["env"]["GH_TOKEN"] == _PAT
    assert launch["workdir"] == "/workspace/api"
    # The prompt (claude reads it from stdin) is augmented with the PR workflow.
    assert "pull request" in launch["stdin"].lower()
    assert "--base main" in launch["stdin"]

    types = [et for et, _ in h._store.events]
    assert EventType.CODE_RUN_RESULT in types
    # The PAT never lands in an event.
    for _et, data in h._store.events:
        assert _PAT not in json.dumps(data)


async def test_code_selects_requested_repo():
    repos = (
        {"url": "https://github.com/acme/api", "default_branch": "main"},
        {"url": "https://github.com/acme/web", "default_branch": "trunk"},
    )
    pool = _FakeSandboxPool(_done_polls())
    h = _Harness(vault=_vault(git_pat=_PAT), sandbox_pool=pool)

    await h._handle_code_command(
        _session(repos), '/code claude "fix X" --repo web', _lease(),
    )
    launch = next(p for a, p in pool.calls if a == "launch")
    assert launch["workdir"] == "/workspace/web"
    assert "--base trunk" in launch["stdin"]


async def test_code_without_repo_keeps_today_behavior():
    pool = _FakeSandboxPool(_done_polls())
    h = _Harness(vault=_vault(git_pat=_PAT), sandbox_pool=pool)

    await h._handle_code_command(_session([]), '/code claude "just look"', _lease())

    actions = [a for a, _ in pool.calls]
    assert "checkout" not in actions
    launch = next(p for a, p in pool.calls if a == "launch")
    assert "GH_TOKEN" not in launch["env"]
    assert "workdir" not in launch


async def test_code_requested_repo_no_match_guidance():
    repo = {"url": "https://github.com/acme/api", "default_branch": "main"}
    pool = _FakeSandboxPool([])
    h = _Harness(vault=_vault(git_pat=_PAT), sandbox_pool=pool)

    await h._handle_code_command(
        _session([repo]), '/code claude "fix" --repo web', _lease(),
    )
    assert not pool.calls  # never launched
    assert "no configured repository matches" in _last_message(h).lower()


async def test_code_repo_without_pat_guidance():
    repo = {"url": "https://github.com/acme/api", "default_branch": "main"}
    pool = _FakeSandboxPool([])
    h = _Harness(vault=_vault(), sandbox_pool=pool)  # no git_pat

    await h._handle_code_command(_session([repo]), '/code claude "fix"', _lease())
    assert not pool.calls
    assert "connect a github token" in _last_message(h).lower()


async def test_code_repo_rejects_read_only():
    repo = {"url": "https://github.com/acme/api", "default_branch": "main"}
    pool = _FakeSandboxPool([])
    h = _Harness(vault=_vault(git_pat=_PAT), sandbox_pool=pool)

    await h._handle_code_command(
        _session([repo]), '/code claude "fix" --allow read-only', _lease(),
    )
    assert not pool.calls
    assert "read-only" in _last_message(h).lower()
