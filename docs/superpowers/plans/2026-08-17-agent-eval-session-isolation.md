# Agent Evaluation Session Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an API-channel session run as an isolated evaluation session: its own throwaway memory partition, no tool that waits on a human, and the ability to start from a pre-written transcript.

**Architecture:** Three independent changes to existing seams. A session created on the `api` channel may declare an `eval_run_id` in its config. The server derives a memory boundary from it (never trusting a client-supplied boundary), the worker's tool filter drops `ask_user_question` for such sessions, and `create_api_session` can seed prior turns as events without waking the worker.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, pytest with `asyncio_mode = "auto"`.

**Spec:** The cross-repository design lives in the ops repository at `docs/superpowers/specs/2026-08-17-agent-eval-openai-facade-design.md`. The context needed to review this plan on its own is in "Why" below.

## Why

Evaluations can target an agent instead of a model. Ops is building an
OpenAI-compatible facade that turns one benchmark row into one real agent
session. That only produces trustworthy scores if the sessions are isolated
from each other and from the live agent, which today they are not:

- An `api`-channel session has `user_id = None` and no memory boundary, so it
  reads and writes the agent's **shared** memory. One benchmark row can write
  a memory a later row reads, making scores order-dependent, and running an
  evaluation permanently edits a customer's live agent memory.
- `ask_user_question` waits up to 30 minutes for a human answer. In an
  evaluation nobody ever answers, so any row that trips the tool stalls until
  the caller times out.
- A multi-turn benchmark resends its whole conversation on each call. Without
  a way to write a transcript, the only option is re-running earlier turns,
  which gives the agent a history that differs from the one the grader is
  scoring against.

Nothing here changes behaviour for web, Slack, Telegram or ordinary API
sessions. Every change is gated on a session config key that only an
evaluation sets.

## Global Constraints

- Default branch is `master`. Work happens on `feat/agent-eval-session-isolation`.
- Commit messages follow Conventional Commits: `type(scope): subject`.
- Never mention AI tooling in commit messages or any committed artifact.
- The eval boundary namespace is exactly `eval:` and the config key is exactly `eval_run_id`.
- A client-supplied `memory_boundary` is never trusted on any channel.
- Run tests with `uv run pytest <path> -v` from the repository root.

## File Structure

- `surogates/channels/memory_boundary.py` gains the eval namespace rule. It stays the single source of truth for boundary resolution.
- `surogates/api/routes/sessions.py` derives the boundary server-side at session creation and gains the seeding path.
- `surogates/orchestrator/worker.py` gains the tool exclusion in the existing `_filter_effective_tools`.
- Tests go beside their existing neighbours: `tests/test_memory_boundary.py` already covers boundary resolution, and new files cover the route and worker behaviour.

---

### Task 1: Derive an evaluation memory boundary server-side

**Files:**
- Modify: `surogates/channels/memory_boundary.py:65-90`
- Modify: `surogates/api/routes/sessions.py:416-440` (inside `_create_session`)
- Test: `tests/test_memory_boundary.py`
- Test: `tests/test_eval_session_boundary.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the config contract every later task reads. A session config carrying `eval_run_id` (a non-empty string) is an evaluation session. `_create_session` stamps `config["memory_boundary"] = f"eval:{eval_run_id}"` and strips any client-supplied `memory_boundary`. `session_memory_boundary(session)` returns that string for non-managed channels.

- [ ] **Step 1: Write the failing boundary-resolution tests**

Append to `tests/test_memory_boundary.py`:

```python
def test_api_session_honours_an_eval_boundary():
    session = SimpleNamespace(
        channel="api", config={"memory_boundary": "eval:run-1"},
    )
    assert session_memory_boundary(session) == "eval:run-1"


def test_api_session_ignores_a_non_eval_boundary():
    # Fail closed: outside the managed channels only the eval namespace is
    # honoured, so a caller cannot address a Slack conversation's memory.
    session = SimpleNamespace(
        channel="api", config={"memory_boundary": "slack:c:C123"},
    )
    assert session_memory_boundary(session) is None


def test_api_session_without_a_boundary_is_unchanged():
    session = SimpleNamespace(channel="api", config={})
    assert session_memory_boundary(session) is None


def test_web_session_ignores_an_eval_boundary_it_did_not_earn():
    # The stamp is only ever applied to api-channel sessions, but the
    # resolver is the hard boundary, so it is asserted here too.
    session = SimpleNamespace(
        channel="web", config={"memory_boundary": "eval:run-1"},
    )
    assert session_memory_boundary(session) == "eval:run-1"
```

Note on the last test: the resolver treats every non-managed channel the same
way. Restricting the stamp to `api` is the route's job, asserted in Step 5.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_memory_boundary.py -v -k eval`
Expected: FAIL, `session_memory_boundary` returns `None` for the first case.

- [ ] **Step 3: Implement the eval namespace in the resolver**

In `surogates/channels/memory_boundary.py`, add the constant near the top,
below `MANAGED_CHANNELS`:

```python
# Memory-boundary namespace for evaluation sessions. Outside the managed
# channels this is the ONLY prefix honoured: an evaluation needs a scratch
# partition, and letting a caller name any boundary would let it read or
# overwrite the memory of a private conversation on the same agent.
EVAL_BOUNDARY_PREFIX = "eval:"
```

Add it to `__all__`, then replace the early return in
`session_memory_boundary`:

```python
    channel = getattr(session, "channel", None)
    if channel not in MANAGED_CHANNELS:
        cfg = getattr(session, "config", None) or {}
        persisted = str(cfg.get("memory_boundary") or "").strip()
        if persisted.startswith(EVAL_BOUNDARY_PREFIX):
            return persisted
        return None
```

Update the docstring's final sentence to read:

```
    Every non-channel session returns ``None`` so the caller keeps today's
    per-user / shared memory, except an evaluation session, which carries an
    ``eval:`` boundary stamped by the session route.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_memory_boundary.py -v`
Expected: PASS, including the pre-existing cases.

- [ ] **Step 5: Write the failing route test**

Create `tests/test_eval_session_boundary.py`:

```python
"""The eval memory boundary is server-derived, never client-supplied."""
from __future__ import annotations

from surogates.api.routes.sessions import apply_eval_isolation


def test_eval_run_id_produces_a_namespaced_boundary():
    config = apply_eval_isolation({"eval_run_id": "run-1"}, channel="api")
    assert config["memory_boundary"] == "eval:run-1"


def test_client_supplied_boundary_is_stripped():
    config = apply_eval_isolation(
        {"memory_boundary": "slack:c:C123"}, channel="api",
    )
    assert "memory_boundary" not in config


def test_client_boundary_cannot_survive_alongside_an_eval_run_id():
    config = apply_eval_isolation(
        {"eval_run_id": "run-1", "memory_boundary": "slack:c:C123"},
        channel="api",
    )
    assert config["memory_boundary"] == "eval:run-1"


def test_non_api_channel_gets_no_boundary():
    config = apply_eval_isolation({"eval_run_id": "run-1"}, channel="web")
    assert "memory_boundary" not in config


def test_blank_eval_run_id_is_not_a_boundary():
    config = apply_eval_isolation({"eval_run_id": "   "}, channel="api")
    assert "memory_boundary" not in config


def test_ordinary_config_is_untouched():
    config = apply_eval_isolation({"single_session": True}, channel="api")
    assert config == {"single_session": True}
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_session_boundary.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply_eval_isolation'`.

- [ ] **Step 7: Implement the stamp**

In `surogates/api/routes/sessions.py`, add above `_create_session`:

```python
def apply_eval_isolation(config: dict, *, channel: str) -> dict:
    """Return *config* with the evaluation memory boundary resolved.

    The boundary is server-owned. A client-supplied ``memory_boundary`` is
    always dropped, because a caller able to name its own boundary could read
    or overwrite the memory of any conversation on the same agent. An
    ``api``-channel session declaring ``eval_run_id`` instead gets a derived
    ``eval:<run id>`` partition, which starts empty and is discarded when the
    run finishes.
    """
    from surogates.channels.constants import API_CHANNEL
    from surogates.channels.memory_boundary import EVAL_BOUNDARY_PREFIX

    resolved = dict(config)
    resolved.pop("memory_boundary", None)
    if channel != API_CHANNEL:
        return resolved
    run_id = str(resolved.get("eval_run_id") or "").strip()
    if run_id:
        resolved["memory_boundary"] = f"{EVAL_BOUNDARY_PREFIX}{run_id}"
    return resolved
```

Then wire it into `_create_session`, replacing the `config` assignment:

```python
    config = apply_eval_isolation(body.config.copy(), channel=channel)
    if body.system:
        config["system"] = body.system
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_eval_session_boundary.py tests/test_memory_boundary.py -v`
Expected: PASS.

- [ ] **Step 9: Run the surrounding suites for regressions**

Run: `uv run pytest tests/test_worker_memory_keys.py tests/test_worker_memory_gate.py tests/test_workspace_boundary_routing.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add surogates/channels/memory_boundary.py surogates/api/routes/sessions.py \
  tests/test_memory_boundary.py tests/test_eval_session_boundary.py
git commit -m "feat(sessions): give evaluation sessions their own memory partition

An api-channel session declaring eval_run_id now runs against a derived
eval:<run id> memory boundary instead of the agent's shared memory, so one
evaluation row cannot read what another wrote and an evaluation no longer
edits the live agent's memory.

The boundary is server-derived and a client-supplied memory_boundary is
always stripped: a caller able to name its own boundary could read or
overwrite the memory of any conversation on the same agent. Outside the
managed channels the resolver honours only the eval: namespace, so the
stripping and the resolution both fail closed."
```

---

### Task 2: Drop `ask_user_question` for evaluation sessions

**Files:**
- Modify: `surogates/orchestrator/worker.py:235-254` (inside `_filter_effective_tools`)
- Test: `tests/test_eval_session_tools.py` (create)

**Interfaces:**
- Consumes: the `eval_run_id` config key established in Task 1.
- Produces: `is_eval_session(session) -> bool` exported from `surogates/orchestrator/worker.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_session_tools.py`:

```python
"""An evaluation session never sees a tool that waits on a human."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.orchestrator.worker import (
    _filter_effective_tools,
    is_eval_session,
)

_TOOLS = {"ask_user_question", "memory", "web_search", "skill_manage"}


def _tenant():
    return SimpleNamespace(user_id=None, service_account_id="sa-1")


def _session(config):
    return SimpleNamespace(channel="api", config=config)


def test_eval_session_is_detected_by_run_id():
    assert is_eval_session(_session({"eval_run_id": "run-1"})) is True


def test_ordinary_api_session_is_not_an_eval_session():
    assert is_eval_session(_session({})) is False


def test_blank_run_id_is_not_an_eval_session():
    assert is_eval_session(_session({"eval_run_id": "  "})) is False


def test_eval_session_loses_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_run_id": "run-1"}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" not in result


def test_eval_session_keeps_every_other_tool():
    # The point is evaluating the real agent, so only the tool that cannot
    # possibly be answered is removed.
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({"eval_run_id": "run-1"}),
        use_api_for_harness_tools=True,
    )
    assert {"memory", "web_search", "skill_manage"} <= result


def test_ordinary_api_session_keeps_ask_user_question():
    result = _filter_effective_tools(
        tools=_TOOLS,
        tenant=_tenant(),
        session=_session({}),
        use_api_for_harness_tools=True,
    )
    assert "ask_user_question" in result
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_session_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_eval_session'`.

- [ ] **Step 3: Implement the predicate and the exclusion**

In `surogates/orchestrator/worker.py`, add above `_filter_effective_tools`:

```python
def is_eval_session(session: Any) -> bool:
    """True when this session is one row of an evaluation run.

    Set by the ops evaluation facade, which opens one session per benchmark
    row. Used to strip tools that cannot complete without a human.
    """
    config = getattr(session, "config", None) or {}
    return bool(str(config.get("eval_run_id") or "").strip())
```

Then add to `_filter_effective_tools`, after the anonymous-channel block:

```python
    # An evaluation row has no human behind it, and ``ask_user_question``
    # blocks the turn for up to 30 minutes waiting for one. Left in place it
    # stalls the row until the caller's timeout, which reads as a hung agent
    # rather than as a tool that could never have been answered.
    if is_eval_session(session):
        result.discard("ask_user_question")
```

Extend the function docstring's numbered rules with:

```
    3. Evaluation sessions never see ``ask_user_question``: no human is
       watching, so the tool can only ever time out.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_eval_session_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Run the neighbouring tool-gating suites**

Run: `uv run pytest tests/test_execution_context_tool_gates.py tests/test_worker_principal_paths.py tests/test_board_tool_gating.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/orchestrator/worker.py tests/test_eval_session_tools.py
git commit -m "feat(worker): drop ask_user_question in evaluation sessions

The tool waits up to 30 minutes for a human answer. An evaluation row has
nobody behind it, so any row that triggers the tool stalls until the caller
times out and reads as a hung agent rather than as a tool that could never
have been answered. Every other tool is kept, since the point is evaluating
the real agent."
```

---

### Task 3: Seed a session with a pre-written transcript

**Files:**
- Modify: `surogates/api/routes/sessions.py` (`CreateSessionRequest`, `create_api_session`)
- Test: `tests/test_eval_session_seeding.py` (create)

**Interfaces:**
- Consumes: `apply_eval_isolation` from Task 1.
- Produces: `CreateSessionRequest.seed_turns: list[SeedTurn] | None`, where `SeedTurn` is `{role: "user" | "assistant", content: str}`. Honoured only by `create_api_session`. Emits one `user.message` event per user turn and one `llm.response` event per assistant turn, in order, and does not enqueue the session.

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_session_seeding.py`:

```python
"""Seeded turns become real events without waking the worker."""
from __future__ import annotations

import pytest

from surogates.api.routes.sessions import SeedTurn, seed_turn_events
from surogates.session.events import EventType


def test_user_turn_becomes_a_user_message_event():
    events = seed_turn_events([SeedTurn(role="user", content="2+2?")])
    assert events == [(EventType.USER_MESSAGE, {"content": "2+2?"})]


def test_assistant_turn_becomes_an_llm_response_event():
    # The response contract is {"message": {"content": ...}}; a bare
    # {"content": ...} would not be read back as an assistant message.
    events = seed_turn_events([SeedTurn(role="assistant", content="4")])
    assert events == [(EventType.LLM_RESPONSE, {"message": {"content": "4"}})]


def test_order_is_preserved():
    events = seed_turn_events([
        SeedTurn(role="user", content="one"),
        SeedTurn(role="assistant", content="two"),
        SeedTurn(role="user", content="three"),
    ])
    assert [t for t, _ in events] == [
        EventType.USER_MESSAGE,
        EventType.LLM_RESPONSE,
        EventType.USER_MESSAGE,
    ]


def test_empty_seed_produces_no_events():
    assert seed_turn_events([]) == []
    assert seed_turn_events(None) == []


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        SeedTurn(role="system", content="x")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_session_seeding.py -v`
Expected: FAIL with `ImportError: cannot import name 'SeedTurn'`.

- [ ] **Step 3: Implement the schema and the pure mapping**

In `surogates/api/routes/sessions.py`, beside `CreateSessionRequest`:

```python
class SeedTurn(BaseModel):
    """One already-completed turn written into a session at creation.

    Used by the evaluation facade, where a multi-turn benchmark resends its
    whole conversation on every call. Re-running the earlier turns would give
    the agent a history that differs from the one the grader is scoring
    against, so the recorded exchange is written verbatim instead.
    """

    role: Literal["user", "assistant"]
    content: str


class CreateSessionRequest(BaseModel):
    system: str | None = None
    config: dict = Field(default_factory=dict)
    seed_turns: list[SeedTurn] | None = None


def seed_turn_events(
    turns: list[SeedTurn] | None,
) -> list[tuple[EventType, dict]]:
    """Map seeded turns onto the event shapes the runtime already stores.

    An assistant turn is wrapped as ``{"message": {"content": ...}}`` because
    that is the ``llm.response`` contract every reader expects; a bare
    ``content`` key would store an event nothing reads back as an answer.
    """
    events: list[tuple[EventType, dict]] = []
    for turn in turns or []:
        if turn.role == "user":
            events.append((EventType.USER_MESSAGE, {"content": turn.content}))
        else:
            events.append(
                (EventType.LLM_RESPONSE, {"message": {"content": turn.content}})
            )
    return events
```

Add `Literal` to the `typing` import at the top of the module.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_eval_session_seeding.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing route test**

Append to `tests/test_eval_session_seeding.py`:

```python
class _RecordingStore:
    def __init__(self):
        self.emitted = []

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((event_type, data))
        return len(self.emitted)


async def test_seeded_turns_are_emitted_in_order():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(
        store,
        session_id="s-1",
        turns=[
            SeedTurn(role="user", content="one"),
            SeedTurn(role="assistant", content="two"),
        ],
    )
    assert store.emitted == [
        (EventType.USER_MESSAGE, {"content": "one"}),
        (EventType.LLM_RESPONSE, {"message": {"content": "two"}}),
    ]


async def test_no_seed_emits_nothing():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(store, session_id="s-1", turns=None)
    assert store.emitted == []
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_session_seeding.py -v -k emit`
Expected: FAIL with `ImportError: cannot import name 'emit_seed_turns'`.

- [ ] **Step 7: Implement the emitter and wire it into the API route**

In `surogates/api/routes/sessions.py`, below `seed_turn_events`:

```python
async def emit_seed_turns(store, *, session_id, turns) -> None:
    """Write seeded turns onto a session without enqueueing it.

    Deliberately no ``enqueue_session`` call: seeding records what already
    happened, so the worker must not wake and answer the last seeded message.
    The agent runs only when the caller sends the real turn afterwards.
    """
    for event_type, data in seed_turn_events(turns):
        await store.emit_event(session_id, event_type, data)
```

In `create_api_session`, after the `_create_session` call produces `session`
and before returning it:

```python
    session = await _create_session(
        body,
        request,
        tenant,
        agent_runtime.agent_id,
        channel=channel,
        user_id=None,
        service_account_id=service_account_id,
    )
    await emit_seed_turns(
        _get_session_store(request),
        session_id=session.id,
        turns=body.seed_turns,
    )
    return session
```

Adjust the surrounding lines to match the existing body of
`create_api_session` rather than replacing logic it already has; the only
addition is the `emit_seed_turns` call between creation and return.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_eval_session_seeding.py -v`
Expected: PASS.

- [ ] **Step 9: Confirm the web path ignores seeding**

Append to `tests/test_eval_session_seeding.py`:

```python
def test_web_create_session_does_not_seed():
    # Only create_api_session seeds. The web route builds a session for a
    # human, where a caller-written transcript would be a forgery.
    import inspect

    from surogates.api.routes import sessions

    assert "emit_seed_turns" not in inspect.getsource(sessions.create_session)
```

Run: `uv run pytest tests/test_eval_session_seeding.py -v`
Expected: PASS.

- [ ] **Step 10: Run the session route suites for regressions**

Run: `uv run pytest tests/api -v -k session`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add surogates/api/routes/sessions.py tests/test_eval_session_seeding.py
git commit -m "feat(sessions): let an API session start from a recorded transcript

create_api_session accepts seed_turns and writes them as user.message and
llm.response events without enqueueing the session, so the agent does not
answer the last seeded message.

A multi-turn benchmark resends its whole conversation on every call. Without
this the only option is re-running the earlier turns, which produces an
answer that differs from the one the grader recorded and is scoring against,
so the model would reason over a history the grader does not share. Only the
service-account route seeds; on the web route a caller-written transcript
would be a forgery."
```

---

## Verification before the pull request

Run the full suite once: `uv run pytest -q`. Two things to check by hand,
because no unit test covers them:

- A session created without `eval_run_id` still resolves to the same memory
  key as before the change. Compare `_build_r2_memory_keys` output for an
  ordinary `api` session against `master`.
- An evaluation session's memory object lands under
  `boundaries/eval:<run id>/memory.json` and the agent's `shared/memory.json`
  is untouched after a seeded session runs.
- Two evaluation sessions carrying the same `eval:<run id>` boundary resolve to
  *different* workspace prefixes. The boundary is memory-only: sharing it with
  the workspace would let a file one benchmark row writes appear in the next,
  which is the contamination this work exists to remove. Check
  `boundary_workspace_prefix` for both sessions and confirm neither is
  `boundaries/eval:<run id>/workspace/`.

The facade that drives all of this lives in the ops repository, so end-to-end
verification happens there once both branches run together.
