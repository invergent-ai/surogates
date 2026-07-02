"""Tests for the repo-checkout path of the shared coding-run core.

A configured repo + git PAT makes ``execute_coding_run`` clone the repo on a
fresh branch (via the ``_code`` checkout action) and run the CLI inside that
checkout — with the git token injected only over the exec channel, never into
an emitted event.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.coding_agents.credentials import CodingAgentCredentials, CredentialBundle
from surogates.coding_agents.run_core import execute_coding_run
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PAT = "github_pat_11ABCDE0secretmiddlepart9876"


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


def _sbx(polls, *, checkout_ok=True):
    calls = []

    async def execute(name, input_json):
        payload = json.loads(input_json)
        calls.append((payload["action"], payload))
        if payload["action"] == "checkout":
            return json.dumps({
                "ok": checkout_ok, "run_id": payload["run_id"],
                "exit_code": 0 if checkout_ok else 128,
                "output": "", "error": None if checkout_ok else "fatal: repo not found",
            })
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


def _anthropic_creds():
    return CodingAgentCredentials(_FakeVault({
        "code_cred:anthropic": CredentialBundle(
            provider="anthropic", auth_mode="oauth",
            token_kind="setup_token", oauth_token="sk-ant-oat01-x",
        ).to_json(),
    }))


def _done_poll():
    return [{
        "ok": True, "done": True, "exit_code": 0, "offset": 40,
        "new_output": json.dumps({
            "type": "result", "result": "Opened PR.",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }) + "\n",
    }]


async def test_checkout_runs_before_launch_and_injects_git_env():
    store = _FakeStore()
    execute, calls = _sbx(_done_poll())
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=_anthropic_creds(), agent="claude", provider="anthropic",
        prompt="fix the login bug", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
        repo={"url": "https://github.com/acme/api", "default_branch": "main"},
        git_pat=_PAT, now=1_700_000_000.0,
    )

    assert outcome.status == "ok"
    assert outcome.result.final_message == "Opened PR."

    # Checkout runs first, then launch.
    actions = [a for a, _ in calls]
    assert actions[0] == "checkout"
    assert "launch" in actions
    assert actions.index("checkout") < actions.index("launch")

    # The clone script references $GH_TOKEN, never the literal PAT.
    checkout = next(p for a, p in calls if a == "checkout")
    assert "$GH_TOKEN" in checkout["command"]
    assert _PAT not in checkout["command"]
    assert checkout["env"]["GH_TOKEN"] == _PAT

    # The git token is merged into the launch env so `git push` works.
    launch = next(p for a, p in calls if a == "launch")
    assert launch["env"]["GH_TOKEN"] == _PAT
    # And the CLI runs inside the checked-out repo directory.
    assert launch["workdir"] == "/workspace/api"

    # Branch + checkout dir surfaced on the outcome.
    assert outcome.branch.startswith("fix/fix-the-login-bug-")
    assert outcome.checkout_dir == "/workspace/api"

    # The PAT never lands in an emitted event.
    for _et, data in store.events:
        assert _PAT not in json.dumps(data)


async def test_caller_supplied_branch_is_used():
    store = _FakeStore()
    execute, calls = _sbx(_done_poll())
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=_anthropic_creds(), agent="claude", provider="anthropic",
        prompt="fix the login bug", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
        repo={"url": "https://github.com/acme/api", "default_branch": "main"},
        git_pat=_PAT, branch="fix/explicit-123", now=1_700_000_000.0,
    )
    assert outcome.branch == "fix/explicit-123"
    checkout = next(p for a, p in calls if a == "checkout")
    assert "fix/explicit-123" in checkout["command"]


async def test_checkout_failure_emits_error_and_skips_launch():
    store = _FakeStore()
    execute, calls = _sbx(_done_poll(), checkout_ok=False)
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=_anthropic_creds(), agent="claude", provider="anthropic",
        prompt="fix it", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
        repo={"url": "https://github.com/acme/api", "default_branch": "main"},
        git_pat=_PAT, now=1_700_000_000.0,
    )

    assert outcome.status == "ok"
    # The CLI is never launched when the checkout fails.
    assert [a for a, _ in calls] == ["checkout"]
    # The failure is surfaced on the result and in a CODE_RUN_RESULT event.
    assert outcome.result.error == "fatal: repo not found"
    result_evt = next(
        d for et, d in store.events if et == EventType.CODE_RUN_RESULT
    )
    assert result_evt["error"] == "fatal: repo not found"


async def test_no_repo_run_never_checks_out():
    store = _FakeStore()
    execute, calls = _sbx(_done_poll())
    outcome = await execute_coding_run(
        store=store, tenant=_tenant(), session=SimpleNamespace(id=uuid4()),
        credentials=_anthropic_creds(), agent="claude", provider="anthropic",
        prompt="just review", model=None, effort=None, read_only=False,
        ensure_sandbox=_noop_ensure, execute=execute, should_cancel=lambda: False,
    )

    assert outcome.status == "ok"
    assert "checkout" not in [a for a, _ in calls]
    assert outcome.branch is None
    assert outcome.checkout_dir is None
    launch = next(p for a, p in calls if a == "launch")
    assert "workdir" not in launch
    assert "GH_TOKEN" not in launch["env"]
