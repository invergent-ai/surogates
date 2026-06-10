# Coding Crew on the Kanban Board — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LLM invoke a coding agent as a tool (`run_coding_agent`), then seed three AgentDefs so a `code-orchestrator` decomposes a build goal into a fixed implement→review→fix→verify kanban chain routed to Claude Code and Codex on a shared workspace.

**Architecture:** Extract the existing `/code` run engine into a standalone, dependency-injected `execute_coding_run` core used by both the slash handler and a new worker-local `run_coding_agent` tool (mirrors `consult_expert`/`delegate_task`). The kanban task layer, orchestrator/worker skills, and workspace inheritance already exist — only the tool + three catalog presets are new.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio (`asyncio_mode = "auto"`), the existing `surogates.coding_agents` runner/agents/credentials modules, the `surogates.tools` registry, AgentDef catalog (AGENT.md-style entries).

**Spec:** `docs/superpowers/specs/2026-06-10-code-crew-kanban-design.md`.

**Conventions (read before starting):**
- Run unit tests: `.venv/bin/python -m pytest tests/<file> -v` from `/work/surogates`.
- Do **not** use `uv run` (reinstalls the pinned wheel over the local dev install).
- Commit messages: no `Co-Authored-By` trailer; no Plan/Task/Phase numbers in the message body.

## Progress

- [ ] Task 1: Extract `execute_coding_run` shared core + rewire the slash handler
- [ ] Task 2: `run_coding_agent` tool (schema + handler + registration)
- [ ] Task 3: Make the tool dispatchable (HARNESS location) + toolset wiring
- [ ] Task 4: Seed the three AgentDefs (claude-coder, codex-reviewer, code-orchestrator)
- [ ] Task 5: Live dry-run on the dev cluster + capture demo artifacts

---

### Task 1: Extract the shared run core

Today the run logic lives on the mixin (`surogates/harness/loop_code_commands.py::_run_code_agent`) bound to `self._store/_tenant/_sandbox_pool`. Extract the engine into a standalone, injectable function so the new tool can reuse it verbatim. The slash handler keeps its idempotency + message/cursor behavior and delegates the run itself.

**Files:**
- Create: `surogates/coding_agents/run_core.py`
- Modify: `surogates/harness/loop_code_commands.py`
- Test: `tests/test_coding_agents_run_core.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_agents_run_core.py`:

```python
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
    )
    assert outcome.status == "ok"
    assert outcome.result.final_message == "Built it."
    assert outcome.result_event_id is not None
    types = [et for et, _ in store.events]
    assert EventType.CODE_RUN_STARTED in types
    assert EventType.CODE_RUN_RESULT in types
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_run_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.coding_agents.run_core'`

- [ ] **Step 3: Write `run_core.py`**

Create `surogates/coding_agents/run_core.py`:

```python
"""Dependency-injected core that runs one coding agent and emits events.

Shared by the ``/code`` slash handler and the ``run_coding_agent`` tool so the
two never diverge.  Callers supply the side-effecting collaborators (sandbox
exec, sandbox-ensure, interrupt check); this module owns the credential ->
invocation -> launch/poll/stream -> result + codex write-back sequence and the
CODE_RUN_* event emission.  Credentials are never placed in an event payload.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from surogates.coding_agents.agents import CodeResult, build_invocation
from surogates.coding_agents.credentials import CodingAgentCredentials, CredentialBundle
from surogates.coding_agents.runner import run_code_agent
from surogates.session.events import EventType


@dataclass
class CodingRunOutcome:
    status: str  # "ok" | "not_connected"
    result: CodeResult | None = None
    result_event_id: int | None = None


def credential_env(bundle: CredentialBundle) -> tuple[dict[str, str], str | None]:
    """Map a credential bundle to a launch env + optional codex auth.json."""
    if bundle.provider == "anthropic":
        if bundle.auth_mode == "oauth":
            return {"CLAUDE_CODE_OAUTH_TOKEN": bundle.oauth_token or ""}, None
        return {"ANTHROPIC_API_KEY": bundle.api_key or ""}, None
    if bundle.auth_mode == "oauth":
        return {}, json.dumps(bundle.auth_json or {})
    return {"OPENAI_API_KEY": bundle.api_key or ""}, None


async def execute_coding_run(
    *,
    store,
    tenant,
    session,
    credentials: CodingAgentCredentials,
    agent: str,
    provider: str,
    prompt: str,
    model: str | None,
    effort: str | None,
    read_only: bool,
    ensure_sandbox: Callable[[], Awaitable[None]],
    execute: Callable[[str, str], Awaitable[str]],
    should_cancel: Callable[[], bool],
    started_metadata: dict | None = None,
) -> CodingRunOutcome:
    bundle = await credentials.load(
        org_id=tenant.org_id, user_id=tenant.user_id, provider=provider,
    )
    if bundle is None:
        return CodingRunOutcome(status="not_connected")

    invocation = build_invocation(
        agent, prompt, model=model, effort=effort, read_only=read_only,
    )
    env, codex_auth_json = credential_env(bundle)

    run_id = uuid4().hex
    started_data = {
        "run_id": run_id, "agent": agent, "provider": provider, "prompt": prompt,
    }
    if started_metadata:
        # e.g. the slash path passes {"source_event_id": ...} for crash-recovery
        # idempotency; the tool path passes nothing (tool-call replay covers it).
        started_data.update(started_metadata)
    await store.emit_event(session.id, EventType.CODE_RUN_STARTED, started_data)

    await ensure_sandbox()

    async def _emit_progress(chunk: str) -> None:
        await store.emit_event(
            session.id,
            EventType.CODE_RUN_PROGRESS,
            {"run_id": run_id, "agent": agent, "chunk": chunk},
        )

    import asyncio

    result = await run_code_agent(
        run_id=run_id,
        agent=agent,
        invocation=invocation,
        env=env,
        codex_auth_json=codex_auth_json,
        execute=execute,
        emit_progress=_emit_progress,
        should_cancel=should_cancel,
        sleep=asyncio.sleep,
    )

    if provider == "openai" and result.updated_codex_auth_json:
        try:
            parsed = json.loads(result.updated_codex_auth_json)
            if isinstance(parsed, dict):
                await credentials.store(
                    org_id=tenant.org_id, user_id=tenant.user_id,
                    bundle=CredentialBundle(
                        provider="openai", auth_mode="oauth", auth_json=parsed,
                    ),
                )
        except (json.JSONDecodeError, TypeError):
            pass

    result_event_id = await store.emit_event(
        session.id,
        EventType.CODE_RUN_RESULT,
        {
            "run_id": run_id, "agent": agent,
            "final_message": result.final_message, "error": result.error,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        },
    )
    return CodingRunOutcome(
        status="ok", result=result, result_event_id=result_event_id,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_run_core.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Rewire the slash handler to use the core**

In `surogates/harness/loop_code_commands.py`, replace the **entire `_run_code_agent` method** with this version (pre-checks → idempotency → core → outcome handling). The credential→env mapping, invocation build, event emission, runner drive, and codex write-back now all live in the core:

```python
    async def _run_code_agent(self, session, cmd, lease, all_events) -> None:
        from surogates.coding_agents.run_core import execute_coding_run

        creds = self._code_credentials()
        if creds is None:
            await self._emit_code_message(session, _NO_VAULT, lease)
            return
        sandbox_pool = getattr(self, "_sandbox_pool", None)
        if sandbox_pool is None:
            await self._emit_code_message(session, _NO_SANDBOX, lease)
            return

        # Idempotency: a crash-recovery re-wake replays the same user.message;
        # if a run for this source event already started, do not relaunch.
        source_event_id = _latest_user_event_id(all_events)
        if source_event_id is not None and _code_run_already_started(
            all_events, source_event_id,
        ):
            return

        from surogates.sandbox.pool import sandbox_session_key

        sandbox_owner = sandbox_session_key(session)

        async def _ensure() -> None:
            await self._ensure_code_sandbox(session, sandbox_owner)

        async def _execute(name: str, input_json: str) -> str:
            return await sandbox_pool.execute(sandbox_owner, name, input_json)

        try:
            outcome = await execute_coding_run(
                store=self._store, tenant=self._tenant, session=session,
                credentials=creds, agent=cmd.agent, provider=cmd.provider,
                prompt=cmd.prompt, model=cmd.flags.get("model"),
                effort=cmd.flags.get("effort"),
                read_only=cmd.flags.get("allow") == "read-only",
                ensure_sandbox=_ensure, execute=_execute,
                should_cancel=lambda: bool(
                    getattr(self, "_interrupt_requested", False)
                ),
                started_metadata={"source_event_id": source_event_id},
            )
        except Exception as exc:  # provisioning/build failure — report cleanly
            logger.warning("/code run failed: %s", exc)
            await self._emit_code_message(
                session, f"Could not run {cmd.agent}: {exc}", lease,
            )
            return

        if outcome.status == "not_connected":
            await self._emit_code_message(
                session, render_connect_first(cmd.agent), lease,
            )
            return

        # The core already emitted CODE_RUN_RESULT — advance the cursor through
        # it so this terminal slash turn is durably processed.
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=outcome.result_event_id,
            lease_token=lease.lease_token,
        )
```

Then delete the helpers the core now subsumes, and their now-unused imports:
- **Delete** `_credential_env` (now `credential_env` in the core), `_emit_code_result`, `_writeback_codex_auth`, and the `_async_sleep` helper.
- **Delete** the module-level `build_invocation` and `run_code_agent` imports if nothing else references them.
- **Keep** `_emit_code_message`, `_ensure_code_sandbox`, `_latest_user_event_id`, `_code_run_already_started`, `_code_credentials`, and the help/status/login/logout flow — all unchanged.

- [ ] **Step 6: Run the existing slash-path tests to verify no regression**

Run: `.venv/bin/python -m pytest tests/test_code_command_mixin.py -v`
Expected: PASS (all). If a test referenced a deleted helper, update it to assert via emitted events instead.

- [ ] **Step 7: Commit**

```bash
git add surogates/coding_agents/run_core.py surogates/harness/loop_code_commands.py tests/test_coding_agents_run_core.py tests/test_code_command_mixin.py
git commit -m "refactor(code): extract shared execute_coding_run core; slash handler delegates to it"
```

---

### Task 2: The `run_coding_agent` tool

A worker-local tool the LLM can call. Mirrors `surogates/tools/builtin/expert.py` (schema + `register` + async handler taking `**kwargs`).

**Files:**
- Create: `surogates/tools/builtin/coding_agent.py`
- Test: `tests/test_coding_agent_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_agent_tool.py`:

```python
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
    monkeypatch.setattr(mod, "_build_ensure", lambda sp, s, t, owner: (lambda: _aw_none()))

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


async def _aw_none():
    return None


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coding_agent_tool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.tools.builtin.coding_agent'`

- [ ] **Step 3: Write the tool**

Create `surogates/tools/builtin/coding_agent.py`:

```python
"""Built-in ``run_coding_agent`` tool — run Claude Code / Codex from the LLM.

Worker-local (mirrors ``consult_expert``).  The LLM hands a task to an external
coding agent running on the *user's own* connected plan inside the session
sandbox; the streamed run renders as a CodeRunBlock and the final message is
returned to the calling LLM so it can act on it.

Required kwargs (injected by the harness dispatch): ``tenant``, ``session_id``,
``session_store``, ``sandbox_pool``, ``credential_vault``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from surogates.coding_agents.command import AGENT_TO_PROVIDER
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.run_core import execute_coding_run
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_SCHEMA = ToolSchema(
    name="run_coding_agent",
    description=(
        "Run an external coding agent (Claude Code or Codex) on the current "
        "workspace using the user's connected plan. Use 'claude' to implement "
        "or edit code, 'codex' to review and run tests. The agent works on the "
        "shared /workspace and returns its final message. One run is one shot — "
        "it cannot pause to ask questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": ["claude", "codex"]},
            "prompt": {"type": "string", "description": "The task for the coding agent."},
            "model": {"type": "string"},
            "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]},
            "read_only": {"type": "boolean", "description": "Run without writing changes."},
        },
        "required": ["agent", "prompt"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="run_coding_agent",
        schema=_SCHEMA,
        handler=_run_coding_agent_handler,
        toolset="code",
    )


def _build_ensure(sandbox_pool, session, tenant, owner) -> Callable[[], Awaitable[None]]:
    async def _ensure() -> None:
        from surogates.harness.tool_exec import _build_session_sandbox_spec

        spec = _build_session_sandbox_spec(session, tenant, owner)
        await sandbox_pool.ensure(owner, spec)

    return _ensure


async def _run_coding_agent_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    agent = arguments.get("agent", "")
    prompt = (arguments.get("prompt") or "").strip()
    if agent not in AGENT_TO_PROVIDER:
        return json.dumps({"error": f"Unknown agent {agent!r}; use 'claude' or 'codex'."})
    if not prompt:
        return json.dumps({"error": "prompt is required"})

    tenant = kwargs.get("tenant")
    session_id = kwargs.get("session_id")
    store = kwargs.get("session_store")
    sandbox_pool = kwargs.get("sandbox_pool")
    vault = kwargs.get("credential_vault")
    if tenant is None or tenant.user_id is None:
        return json.dumps({"error": "no end-user identity for coding-agent run"})
    if store is None or sandbox_pool is None:
        return json.dumps({"error": "coding agents are not available on this deployment"})
    if vault is None:
        return json.dumps({"error": "credential vault is not configured"})

    session = await store.get_session(UUID(str(session_id)))
    if session is None:
        return json.dumps({"error": "session not found"})

    from surogates.sandbox.pool import sandbox_session_key

    owner = sandbox_session_key(session)
    provider = AGENT_TO_PROVIDER[agent]

    async def _execute(name: str, input_json: str) -> str:
        return await sandbox_pool.execute(owner, name, input_json)

    outcome = await execute_coding_run(
        store=store, tenant=tenant, session=session,
        credentials=CodingAgentCredentials(vault),
        agent=agent, provider=provider, prompt=prompt,
        model=arguments.get("model"), effort=arguments.get("effort"),
        read_only=bool(arguments.get("read_only")),
        ensure_sandbox=_build_ensure(sandbox_pool, session, tenant, owner),
        execute=_execute,
        should_cancel=lambda: False,
    )

    if outcome.status == "not_connected":
        return json.dumps({
            "error": f"{agent} is not connected. The user must connect their plan "
                     f"in Settings -> Coding Agents before this can run.",
        })

    r = outcome.result
    return json.dumps({
        "final_message": r.final_message,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "error": r.error,
    })
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coding_agent_tool.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/builtin/coding_agent.py tests/test_coding_agent_tool.py
git commit -m "feat(code): run_coding_agent tool — LLM-invokable Claude Code / Codex runs"
```

---

### Task 3: Make the tool dispatchable (HARNESS location) + toolset wiring

The tool must be registered at startup, run on the HARNESS (worker), not in the sandbox, and receive `credential_vault` in its dispatch kwargs.

**Files:**
- Modify: `surogates/tools/runtime.py` (call `register` in `register_builtins`)
- Modify: `surogates/tools/router.py` (HARNESS location for `run_coding_agent`)
- Modify: `surogates/harness/tool_exec.py` (pass `credential_vault` into the dispatch kwargs)
- Test: `tests/test_coding_agent_tool_wiring.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_agent_tool_wiring.py`:

```python
"""Wiring: the tool is registered, harness-located, and gets the vault kwarg."""

from __future__ import annotations

from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime


def test_tool_is_registered_and_harness_located():
    registry = ToolRegistry()
    runtime = ToolRuntime(registry)
    runtime.register_builtins()
    assert registry.has("run_coding_agent")  # registry.tool_names() is the set

    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    # Worker-local, like consult_expert / delegate_task. Default is SANDBOX,
    # so an explicit HARNESS entry is required.
    assert TOOL_LOCATIONS.get("run_coding_agent") == ToolLocation.HARNESS


def test_dispatch_kwargs_include_credential_vault():
    # The harness dispatch must forward credential_vault so the tool can
    # resolve the user's connected plan.
    import inspect
    import surogates.harness.tool_exec as te

    src = inspect.getsource(te)
    assert "credential_vault=" in src
```

(`ToolLocation` and `TOOL_LOCATIONS` live in `surogates/tools/router.py`; the
registry exposes `has(name)` and `tool_names()`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coding_agent_tool_wiring.py -v`
Expected: FAIL — tool not registered / not HARNESS-located / vault not forwarded.

- [ ] **Step 3: Register the tool**

In `surogates/tools/runtime.py`, inside `register_builtins`, add alongside the other `from surogates.tools.builtin import X; X.register(self._registry)` calls:

```python
        from surogates.tools.builtin import coding_agent
        coding_agent.register(self._registry)
```

(Match the exact registration idiom already used in `register_builtins` — some builtins call `register(self.registry)` vs `self._registry`; mirror the neighbours.)

- [ ] **Step 4: Pin the HARNESS location**

In `surogates/tools/router.py`, add an entry to the `TOOL_LOCATIONS` dict next to the existing `delegate_task` / `consult_expert` entries (resolve_location defaults unclassified tools to `SANDBOX`, which would wrongly route this into the pod):

```python
    "run_coding_agent": ToolLocation.HARNESS,
```

- [ ] **Step 5: Forward the vault into dispatch kwargs**

In `surogates/harness/tool_exec.py`, in the `tools.dispatch(...)` call (the HARNESS-location branch, near `credential_vault` is NOT yet passed), add the kwarg. The harness already holds the vault — thread it the same way `sandbox_pool` is. Add to the dispatch call:

```python
                credential_vault=getattr(self, "_credential_vault", None),
```

If `execute_tool_calls` is a free function (not a method), the vault must be threaded in from the `AgentHarness` caller: add a `credential_vault` parameter to the relevant `execute_tool_calls*` signature and pass `self._credential_vault` at the call site in `loop.py`. Grep `execute_tool_calls(` in `loop.py` to find the call site and thread it through.

- [ ] **Step 6: Run the test + import smoke**

Run: `.venv/bin/python -m pytest tests/test_coding_agent_tool_wiring.py -v`
Expected: PASS (2 passed)

Run: `.venv/bin/python -c "import surogates.harness.loop, surogates.tools.runtime; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
git add surogates/tools/runtime.py surogates/tools/router.py surogates/harness/tool_exec.py surogates/harness/loop.py tests/test_coding_agent_tool_wiring.py
git commit -m "feat(code): register run_coding_agent (harness-local) and forward the credential vault"
```

---

### Task 4: Seed the three AgentDefs

AgentDefs are AGENT.md-style catalog entries (`name`, `tools` allowlist, `disallowed_tools`, `system_prompt` body, optional `model`). They resolve via `resolve_agent_by_name` from the tenant's merged catalog (DB rows + Hub bundle). Seed three for the demo tenant/agent.

**Files:**
- Create: `surogates/environments/coding-crew/claude-coder.md`
- Create: `surogates/environments/coding-crew/codex-reviewer.md`
- Create: `surogates/environments/coding-crew/code-orchestrator.md`
- Test: `tests/test_coding_crew_agentdefs.py`

> Confirm the catalog source first: `grep -n "def resolve_agent_by_name" -A40 surogates/tools/loader.py`. If the demo tenant loads AgentDefs from a Hub bundle rather than local `.md` files, place these three entries in that bundle instead and adjust paths — the *content* below is unchanged.

- [ ] **Step 1: Author `claude-coder.md`**

> Frontmatter note: `_build_agent_def` requires a `description` field (it reads `parsed["description"]`) — every AGENT.md below includes one. Omitting it raises `KeyError`.

```markdown
---
name: claude-coder
description: Implementation specialist — writes and edits code via Claude Code.
tools: [run_coding_agent, read_file, list_files, search_files]
max_iterations: 6
---

You are the implementation specialist on a coding crew. You implement and edit
code by calling `run_coding_agent(agent="claude", prompt="<detailed task>")`,
which runs Claude Code on the shared `/workspace`.

- Do the work through `run_coding_agent`; do not hand-write large code yourself.
- When given review findings (from a parent task), address each one specifically.
- When done, `worker_complete` with a 1-3 sentence summary and metadata listing
  the files you changed and how you verified them.
```

- [ ] **Step 2: Author `codex-reviewer.md`**

```markdown
---
name: codex-reviewer
description: Review-and-test specialist — reviews code and runs tests via Codex.
tools: [run_coding_agent, read_file, list_files, search_files]
max_iterations: 6
---

You are the review-and-test specialist on a coding crew. You review the code in
`/workspace` and run its tests by calling
`run_coding_agent(agent="codex", prompt="<review + run tests + report issues>")`.

- Always run the test suite and report pass/fail counts.
- `worker_complete` with the findings in your summary and metadata: list each
  issue (file + what's wrong) and the test results, so the downstream fix card
  can act on them. Do not block — this is a fixed chain.
```

- [ ] **Step 3: Author `code-orchestrator.md`**

```markdown
---
name: code-orchestrator
description: Routes a build across the coding crew; does not write code itself.
tools: [spawn_task, unblock_task, cancel_task]
disallowed_tools: [terminal, write_file, patch, execute_code, run_coding_agent]
---

You route a software build across a coding crew. You do NOT write code yourself.

Decompose every build goal into a fixed four-card chain and spawn it up front:

1. `spawn_task(goal="implement <the build>", agent_type="claude-coder")`
2. `spawn_task(goal="review the implementation and run the tests", agent_type="codex-reviewer", parents=[implement])`
3. `spawn_task(goal="apply the review findings from your parent task", agent_type="claude-coder", parents=[review])`
4. `spawn_task(goal="re-run the tests and confirm everything passes", agent_type="codex-reviewer", parents=[fix])`

Available sub-agents: `claude-coder` (implements/fixes), `codex-reviewer`
(reviews/tests). Use those exact names. After spawning, summarize the plan to the
user and let the board run.
```

- [ ] **Step 4: Write the resolution test**

Create `tests/test_coding_crew_agentdefs.py`:

```python
"""The three crew AgentDefs parse and expose the expected tool filters."""

from __future__ import annotations

from pathlib import Path

from surogates.tools.loader import _build_agent_def, _parse_agent_frontmatter


def _load(name):
    text = (Path("surogates/environments/coding-crew") / f"{name}.md").read_text()
    parsed = _parse_agent_frontmatter(text, name)
    return _build_agent_def(parsed, source="test")


def test_claude_coder_can_run_coding_agent():
    a = _load("claude-coder")
    assert a.name == "claude-coder"
    assert a.description  # required by _build_agent_def
    assert "run_coding_agent" in (a.tools or [])


def test_codex_reviewer_can_run_coding_agent():
    a = _load("codex-reviewer")
    assert "run_coding_agent" in (a.tools or [])


def test_orchestrator_cannot_run_code_directly():
    a = _load("code-orchestrator")
    assert "spawn_task" in (a.tools or [])
    assert "run_coding_agent" in (a.disallowed_tools or [])
```

(`_parse_agent_frontmatter(text, name)` → dict, then `_build_agent_def(parsed, source)` → `AgentDef`; both are the real loader internals. `_build_agent_def` requires `description`.)

- [ ] **Step 5: Run the test**

Run: `.venv/bin/python -m pytest tests/test_coding_crew_agentdefs.py -v`
Expected: PASS (3 passed). If the parser needs a file path rather than a string, adapt the helper.

- [ ] **Step 6: Register the crew with the demo tenant**

Make the three AgentDefs resolvable for the demo agent: either (a) drop them into the demo agent's Hub bundle under the agent-definitions path the catalog scans, or (b) insert catalog rows via the same path `surogates/api/routes/agents.py::create_agent` uses. Verify:

```bash
.venv/bin/python - <<'PY'
# Resolve against the demo tenant; prints the three names if registered.
# Fill in the demo org/agent ids from ~/.surogate/config.yaml or the DB.
PY
```

Expected: `claude-coder`, `codex-reviewer`, `code-orchestrator` all resolve.

- [ ] **Step 7: Commit**

```bash
git add surogates/environments/coding-crew/ tests/test_coding_crew_agentdefs.py
git commit -m "feat(code): seed claude-coder / codex-reviewer / code-orchestrator agent defs"
```

---

### Task 5: Live dry-run + demo artifacts

This is the showcase rehearsal and doubles as the vendor-CLI isolation check.

**Prereqs:** dev cluster up (`bash k8s/setup-cluster.sh` already run), the sandbox image rebuilt with the `claude`/`codex` CLIs and imported into k3d, surogates `api` + `worker` running on this branch, the web app served.

- [ ] **Step 1: Rebuild + load the sandbox image**

```bash
docker build -t surogates-sandbox:codecrew images/sandbox/
k3d image import surogates-sandbox:codecrew -c <your-k3d-cluster>
```
Point the sandbox config at that tag (the sandbox image ref in `config.dev.yaml` / the sandbox spec). Expected: pods come up with `claude --version` / `codex --version` resolvable (verify with a one-off `/code claude "print your version"`).

- [ ] **Step 2: Connect both plans**

In the web app: Settings → Coding Agents → connect Claude (`sk-ant-oat…`) and Codex (`~/.codex/auth.json`). Expected: `/code status` shows both connected.

- [ ] **Step 3: Run the crew**

Start a session on the `code-orchestrator` agent and send:
> Build a working URL-shortener (small Flask API + one HTML page) in the workspace — implemented, reviewed, and tested.

Expected on screen: four cards appear (implement → review → fix → verify), light up left to right; a `CodeRunBlock` streams under the implement and review cards; the workspace tree fills; `verify` goes green.

- [ ] **Step 4: Isolation spot-check (security gate)**

While a run is active, in a separate `/code claude "print env and try to read /tmp/.code-runs and ~/.codex/auth.json"` confirm the injected token is NOT readable by agent-run code (SRT deny-read patterns hold). Expected: reads of `/tmp/.code-runs` / `auth.json` are denied; no token-shaped strings in any emitted event.

- [ ] **Step 5: Capture artifacts**

Screenshot the finished board + both `CodeRunBlock`s + the workspace tree + the running app. Save under `docs/superpowers/specs/assets/` (create the dir). These are the showcase materials.

- [ ] **Step 6: Commit the artifacts + a short runbook**

```bash
git add docs/superpowers/specs/assets/ docs/superpowers/specs/2026-06-10-code-crew-kanban-design.md
git commit -m "docs(code): coding-crew demo dry-run artifacts and runbook"
```

---

## Final Verification

- [ ] `.venv/bin/python -m pytest tests/test_coding_agents_run_core.py tests/test_coding_agent_tool.py tests/test_coding_agent_tool_wiring.py tests/test_coding_crew_agentdefs.py tests/test_code_command_mixin.py -v` — all PASS.
- [ ] `.venv/bin/python -c "import surogates.harness.loop, surogates.tools.runtime, surogates.api.app; print('ok')"` — `ok`.
- [ ] The slash path still works (`/code claude "…"` runs and renders) — verified by the unchanged `test_code_command_mixin.py` plus a manual run.
- [ ] The full coding-agent suite shows no regressions vs the pre-change baseline.

## What ships after this plan

The LLM can run Claude Code and Codex via `run_coding_agent`; a `code-orchestrator` turns one build goal into a fixed implement→review→fix→verify kanban chain on a shared workspace; the demo runs end to end on the dev cluster with captured artifacts.
