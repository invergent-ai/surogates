# Loop-result inline delivery (web/api) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface each web/api-originated loop run's final answer inline in the originating conversation by emitting a new `loop.result` event on the parent session, and stop creating the redundant expiring inbox card for those runs.

**Architecture:** At loop-run completion the harness emits a new `loop.result` event onto the *parent* web/api session's event log (never the outbox, never a parent wake). `emit_event` already publishes to `surogates:session:{id}`, so the parent's SSE/poll stream delivers it. Because `loop.result` is a new event type, the context-replay whitelist ignores it, keeping the parent agent's context untouched. Frontends render it as an assistant-style message.

**Tech Stack:** Python 3.12 async (surogates harness), pytest + testcontainers Postgres, TypeScript/React (`sdk/agent-chat-react`, `web`, `surogate-ops/frontend`), SQL view (`surogates/db/observability.sql`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-loop-result-inline-delivery-design.md` — authoritative.
- Run backend tests with the repo venv: `/work/surogates/.venv/bin/pytest`. Never `uv run` (it reinstalls pinned wheels).
- Conventional Commits for every commit (`type(scope): subject`). No `Co-Authored-By` trailer. Never reference plan/task/phase/step numbers in code comments or commit messages.
- Gating is by parent **channel** (`web` or `api`), not by loop kind — cron and dynamic loops both qualify.
- `loop.result` must NOT be added to `SessionStore._DELIVERABLE_EVENTS` and must NOT be consumed by `_rebuild_messages`.
- Emission is best-effort: a failure must never abort run completion, status update, cursor advancement, or dynamic-loop finalization.
- Two repos are touched: `/work/surogates` (backend, SDK, web host, SQL view) and `/work/surogate-ops` (ops frontend). Commit within each repo separately.

---

## File Structure

Backend (`/work/surogates`):
- `surogates/session/events.py` — add `EventType.LOOP_RESULT`.
- `surogates/harness/loop_messages.py` — add `_resolve_loop_result_parent` helper.
- `surogates/harness/loop_artifact_completion.py` — emit `loop.result`, suppress inbox card, fix cursor fallback.
- `tests/test_loop_result_delivery.py` — new unit tests (gating + emission + suppression + cursor + no-wake).
- `tests/test_loop_result_replay_safety.py` — replay + `_DELIVERABLE_EVENTS` guards.

Frontend SDK (`/work/surogates/sdk/agent-chat-react`):
- `src/types.ts` — add `"loop.result"` to `AgentChatEventType`; add `loopResult?` to `AgentChatMessage`.
- `src/runtime/events.ts` — add `"loop.result"` to `AGENT_CHAT_LISTENED_EVENTS`.
- `src/runtime/reducer.ts` — add `case "loop.result"`.
- `src/components/chat/chat-thread.tsx` — render the loop affordance.
- `src/runtime/reducer.test.ts` (or existing reducer test file) — reducer test.

Web host (`/work/surogates/web`):
- `src/types/session.ts` — add `"loop.result"` to the raw session event type.

Ops (`/work/surogates/surogates/db` + `/work/surogate-ops/frontend`):
- `surogates/db/observability.sql` — add `'loop.result'` to `v_session_messages`.
- `frontend/src/types/session.ts` — add `EventType.LOOP_RESULT`.
- `frontend/src/components/sessions/session-thread.tsx` — render loop.result as an agent turn.

---

## Task 1: Add the `loop.result` event type (backend)

**Files:**
- Modify: `surogates/session/events.py` (near `INBOX_TASK_COMPLETE`, ~line 217)
- Test: `tests/test_loop_result_replay_safety.py`

**Interfaces:**
- Produces: `EventType.LOOP_RESULT` with value `"loop.result"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_loop_result_replay_safety.py`:

```python
from surogates.session.events import EventType
from surogates.session.store import _DELIVERABLE_EVENTS


def test_loop_result_event_type_exists():
    assert EventType.LOOP_RESULT.value == "loop.result"


def test_loop_result_is_not_a_deliverable_event():
    # loop.result is surfaced on web/api parents via SSE, never the outbox.
    assert EventType.LOOP_RESULT not in _DELIVERABLE_EVENTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_replay_safety.py -q`
Expected: FAIL with `AttributeError: LOOP_RESULT`.

- [ ] **Step 3: Add the enum member**

In `surogates/session/events.py`, immediately after the `INBOX_*` block:

```python
    # Loop-run result surfaced inline on a web/api parent conversation.
    LOOP_RESULT = "loop.result"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_replay_safety.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/session/events.py tests/test_loop_result_replay_safety.py
git commit -m "feat(events): add loop.result event type"
```

---

## Task 2: `_resolve_loop_result_parent` gating helper (backend)

**Files:**
- Modify: `surogates/harness/loop_messages.py` (add helper next to `_should_notify_parent_on_completion`, ~line 239)
- Test: `tests/test_loop_result_delivery.py`

**Interfaces:**
- Consumes: a `session` with `.channel`, `.parent_id`; an async `store.get_session(uuid) -> Session` (raises `SessionNotFoundError` when missing).
- Produces: `async def resolve_loop_result_parent(session, store) -> Session | None` — returns the parent `Session` only for a scheduled child (`channel == "scheduled"`) with a `parent_id` whose parent channel is `web` or `api`; otherwise `None` (including lookup failure).

- [ ] **Step 1: Write the failing test**

Create `tests/test_loop_result_delivery.py`:

```python
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_messages import resolve_loop_result_parent
from surogates.session.store import SessionNotFoundError


class _FakeStore:
    def __init__(self, parent):
        self._parent = parent

    async def get_session(self, sid):
        if self._parent is None or self._parent.id != sid:
            raise SessionNotFoundError(str(sid))
        return self._parent


def _child(channel="scheduled", parent_id=None):
    return SimpleNamespace(id=uuid4(), channel=channel, parent_id=parent_id)


def _parent(channel):
    return SimpleNamespace(id=uuid4(), channel=channel, parent_id=None)


@pytest.mark.parametrize("parent_channel", ["web", "api"])
async def test_resolves_parent_for_web_and_api(parent_channel):
    parent = _parent(parent_channel)
    child = _child(parent_id=parent.id)
    got = await resolve_loop_result_parent(child, _FakeStore(parent))
    assert got is parent


@pytest.mark.parametrize("parent_channel", ["slack", "telegram", "teams"])
async def test_skips_channel_parents(parent_channel):
    parent = _parent(parent_channel)
    child = _child(parent_id=parent.id)
    assert await resolve_loop_result_parent(child, _FakeStore(parent)) is None


async def test_skips_non_scheduled_child():
    parent = _parent("web")
    child = _child(channel="worker", parent_id=parent.id)
    assert await resolve_loop_result_parent(child, _FakeStore(parent)) is None


async def test_skips_when_no_parent_id():
    child = _child(parent_id=None)
    assert await resolve_loop_result_parent(child, _FakeStore(None)) is None


async def test_skips_when_parent_missing():
    child = _child(parent_id=uuid4())
    assert await resolve_loop_result_parent(child, _FakeStore(None)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py -q`
Expected: FAIL with `ImportError: cannot import name 'resolve_loop_result_parent'`.

- [ ] **Step 3: Implement the helper**

In `surogates/harness/loop_messages.py`, add (imports `SessionNotFoundError` lazily to avoid a cycle):

```python
async def resolve_loop_result_parent(session: Any, store: Any) -> Any | None:
    """Return the parent session a loop run should surface its result into.

    A scheduled run (``channel == "scheduled"``) whose parent conversation
    is a pull-based surface (``web`` or ``api``) has no outbox path, so its
    per-run answer is delivered by emitting a ``loop.result`` event on the
    parent.  Channel parents (slack/telegram/teams) already receive each run
    through the outbox and are skipped; detached runs (no parent) and missing
    parents return ``None``.
    """
    if session.channel != "scheduled" or session.parent_id is None:
        return None
    from surogates.session.store import SessionNotFoundError

    try:
        parent = await store.get_session(session.parent_id)
    except SessionNotFoundError:
        return None
    if parent is None or parent.channel not in ("web", "api"):
        return None
    return parent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_messages.py tests/test_loop_result_delivery.py
git commit -m "feat(harness): add loop-result parent resolution helper"
```

---

## Task 3: Emit `loop.result`, suppress inbox card, fix cursor fallback (backend)

**Files:**
- Modify: `surogates/harness/loop_artifact_completion.py` (`_complete_session`, lines ~596–662)
- Test: `tests/test_loop_result_delivery.py` (append)

**Interfaces:**
- Consumes: `resolve_loop_result_parent` (Task 2); `EventType.LOOP_RESULT` (Task 1); `self._store.get_events(session_id) -> list[Event]`; `extract_final_response(events, fallback="") -> str`; `self._store.emit_event(session_id, type, data) -> int`.
- Produces: a `loop.result` event on the parent for web/api scheduled runs; no `inbox.task_complete` for those runs; correct cursor advancement.

- [ ] **Step 1: Write the failing test (append to `tests/test_loop_result_delivery.py`)**

```python
from surogates.session.events import EventType


class _RecordingStore:
    """Captures emit_event calls and serves child events for extraction."""

    def __init__(self, parent, child_events):
        self._parent = parent
        self._child_events = child_events
        self.emitted = []          # (session_id, type, data)
        self.enqueued = []         # parent re-enqueue attempts
        self.next_id = 1000

    async def get_session(self, sid):
        if self._parent is not None and self._parent.id == sid:
            return self._parent
        from surogates.session.store import SessionNotFoundError
        raise SessionNotFoundError(str(sid))

    async def get_events(self, sid):
        return self._child_events

    async def emit_event(self, sid, etype, data):
        self.next_id += 1
        self.emitted.append((sid, etype, data))
        return self.next_id

    async def update_session_status(self, *a, **k):
        pass

    async def advance_harness_cursor(self, *a, **k):
        pass


def _evt(etype, content):
    return SimpleNamespace(type=etype.value if hasattr(etype, "value") else etype,
                           data={"message": {"content": content}})


def _bare_harness(store):
    # _complete_session lives on ArtifactCompletionMixin. Build a bare host and
    # set only the attributes the method touches.
    from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin

    h = type("_H", (ArtifactCompletionMixin,), {})()
    h._store = store
    h._worker_id = "w"
    h._sandbox_pool = None
    h._memory_manager = None
    h._turn_summarizer = None
    h._redis = None
    h._session_factory = None
    h._tenant = SimpleNamespace(user_id=uuid4(), service_account_id=None)
    return h
```

Concrete tests (append to the same file):

```python
    parent = _parent("web")
    child = SimpleNamespace(
        id=uuid4(), channel="scheduled", parent_id=parent.id,
        org_id=uuid4(), agent_id="a", title="t",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        config={"scheduled_session_id": str(uuid4())},
        task_id=None,
    )
    store = _RecordingStore(parent, child_events=[_evt(EventType.LLM_RESPONSE, "Weather: 22C")])
    h = _bare_harness(store)

    await h._complete_session(
        child,
        messages=[{"role": "assistant", "content": "Weather: 22C"}],
        lease=SimpleNamespace(lease_token="t"),
        reason="stop",
    )

    loop_results = [e for e in store.emitted if e[1] == EventType.LOOP_RESULT]
    assert len(loop_results) == 1
    sid, _, data = loop_results[0]
    assert sid == parent.id                       # emitted on the PARENT
    assert data["content"] == "Weather: 22C"      # full final response
    assert data["run_session_id"] == str(child.id)
    # inbox card suppressed for this run
    assert not any(e[1] == EventType.INBOX_TASK_COMPLETE for e in store.emitted)


async def test_slack_parent_scheduled_run_keeps_inbox_and_no_loop_result():
    parent = _parent("slack")
    child = SimpleNamespace(
        id=uuid4(), channel="scheduled", parent_id=parent.id,
        org_id=uuid4(), agent_id="a", title="t",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        config={"scheduled_session_id": str(uuid4())}, task_id=None,
    )
    store = _RecordingStore(parent, child_events=[_evt(EventType.LLM_RESPONSE, "hi")])
    h = _bare_harness(store)
    await h._complete_session(child, messages=[{"role": "assistant", "content": "hi"}],
                              lease=SimpleNamespace(lease_token="t"), reason="stop")
    assert not any(e[1] == EventType.LOOP_RESULT for e in store.emitted)
    assert any(e[1] == EventType.INBOX_TASK_COMPLETE for e in store.emitted)
```

Add a `_bare_harness(store)` helper in the test file per the fixture note.

- [ ] **Step 2: Run test to verify it fails**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py -q`
Expected: FAIL — `loop.result` never emitted / inbox still present.

- [ ] **Step 3: Implement in `_complete_session`**

Replace the SESSION_COMPLETE + INBOX_TASK_COMPLETE block (lines ~596–615) and the cursor block (lines ~650–653):

```python
        session_complete_event_id = await self._store.emit_event(
            session.id,
            EventType.SESSION_COMPLETE,
            complete_data,
        )

        outcome = (
            "success"
            if reason in {"stop", "done", "complete", "completed"}
            else reason
        )

        # Surface web/api loop runs inline on the origin conversation instead
        # of an expiring inbox card. Best-effort; never abort completion.
        loop_result_parent = None
        try:
            loop_result_parent = await resolve_loop_result_parent(session, self._store)
        except Exception:
            logger.debug("loop.result parent resolution failed for %s", session.id,
                         exc_info=True)
        if loop_result_parent is not None:
            try:
                child_events = await self._store.get_events(session.id)
                content = extract_final_response(child_events, fallback="").strip()
                if content:
                    await self._store.emit_event(
                        loop_result_parent.id,
                        EventType.LOOP_RESULT,
                        {
                            "run_session_id": str(session.id),
                            "scheduled_session_id": str(
                                session.config.get("scheduled_session_id") or ""
                            ),
                            "content": content,
                            "outcome": outcome,
                            "duration_seconds": _seconds_since(session.created_at),
                            "run_completed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
            except Exception:
                logger.warning("Failed to emit loop.result on parent %s for run %s",
                               loop_result_parent.id, session.id, exc_info=True)

        # Inbox card: skip for web/api loop runs (delivered inline above).
        inbox_event_id: int | None = None
        if loop_result_parent is None:
            inbox_event_id = await self._store.emit_event(
                session.id,
                EventType.INBOX_TASK_COMPLETE,
                {
                    "outcome": outcome,
                    "summary": _last_assistant_message_excerpt(messages),
                    "duration_seconds": _seconds_since(session.created_at),
                    "session_title": session.title or "Task complete",
                    "error": None,
                },
            )
```

And the cursor fallback (lines ~650–653):

```python
        # Advance cursor to the latest child event. When the inbox card is
        # suppressed, fall back to the child's SESSION_COMPLETE event id.
        cursor_target = (
            through_event_id
            if through_event_id is not None
            else (inbox_event_id if inbox_event_id is not None
                  else session_complete_event_id)
        )
```

Add imports at the top of `loop_artifact_completion.py` if absent:

```python
from datetime import datetime, timezone

from surogates.harness.loop_messages import resolve_loop_result_parent
from surogates.harness.message_utils import extract_final_response
```

(`_seconds_since` and `_last_assistant_message_excerpt` already exist in this module — reuse them. There is no `_utcnow` here, so use `datetime.now(timezone.utc)` for `run_completed_at`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py -q`
Expected: PASS.

- [ ] **Step 5: Add cursor + no-wake tests (append) and run**

```python
async def test_parent_not_re_enqueued_by_surfacing():
    parent = _parent("web")
    child = SimpleNamespace(id=uuid4(), channel="scheduled", parent_id=parent.id,
                            org_id=uuid4(), agent_id="a", title="t",
                            created_at=__import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc),
                            config={"scheduled_session_id": str(uuid4())}, task_id=None)
    store = _RecordingStore(parent, [_evt(EventType.LLM_RESPONSE, "x")])
    h = _bare_harness(store)
    await h._complete_session(child, messages=[{"role": "assistant", "content": "x"}],
                              lease=SimpleNamespace(lease_token="t"), reason="stop")
    assert store.enqueued == []   # no parent wake


async def test_empty_final_response_emits_no_loop_result_but_completes():
    parent = _parent("web")
    child = SimpleNamespace(id=uuid4(), channel="scheduled", parent_id=parent.id,
                            org_id=uuid4(), agent_id="a", title="t",
                            created_at=__import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc),
                            config={"scheduled_session_id": str(uuid4())}, task_id=None)
    store = _RecordingStore(parent, child_events=[])   # no llm.response
    h = _bare_harness(store)
    await h._complete_session(child, messages=[], lease=SimpleNamespace(lease_token="t"),
                              reason="stop")
    assert not any(e[1] == EventType.LOOP_RESULT for e in store.emitted)
```

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_artifact_completion.py tests/test_loop_result_delivery.py
git commit -m "feat(harness): deliver web/api loop results inline on the parent conversation"
```

---

## Task 4: Replay-safety guard (backend)

**Files:**
- Test: `tests/test_loop_result_replay_safety.py` (append)

**Interfaces:**
- Consumes: `LoopContextReplay._rebuild_messages` (in `surogates/harness/loop_context_replay.py`) or the class that owns it.

This is a guard test that locks in existing whitelist behavior: `_rebuild_messages`
(on `ContextReplayMixin`) branches on known event types via an if/elif chain, so
`loop.result` — a new type — is never appended to the agent's message history. It
requires no production change and should pass immediately once wired to the real
class.

- [ ] **Step 1: Write the test (append to `tests/test_loop_result_replay_safety.py`)**

```python
from types import SimpleNamespace

from surogates.session.events import EventType


def _event(etype, data, eid):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    from surogates.harness.loop_context_replay import ContextReplayMixin

    host = type("_H", (ContextReplayMixin,), {})()
    return host._rebuild_messages(events)


def test_rebuild_messages_ignores_loop_result():
    events = [
        _event(EventType.LLM_RESPONSE,
               {"message": {"role": "assistant", "content": "real"}}, 1),
        _event(EventType.LOOP_RESULT, {"content": "loop output"}, 2),
    ]
    rebuilt = _rebuild(events)
    joined = " ".join(str(m.get("content", "")) for m in rebuilt)
    assert "real" in joined
    assert "loop output" not in joined
```

- [ ] **Step 2: Run the test**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_replay_safety.py -q`
Expected: PASS (guard test — `loop.result` is not a handled branch, so it is ignored).

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add tests/test_loop_result_replay_safety.py
git commit -m "test(harness): guard that loop.result stays out of replay + outbox"
```

---

## Task 5: SDK type + listened-events registration

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` (`AgentChatEventType` ~line 478; `AgentChatMessage` ~line 61)
- Modify: `sdk/agent-chat-react/src/runtime/events.ts` (`AGENT_CHAT_LISTENED_EVENTS` ~line 11)

**Interfaces:**
- Produces: `"loop.result"` is a valid `AgentChatEventType` and a listened event; `AgentChatMessage.loopResult?` metadata field.

- [ ] **Step 1: Add to the type union**

In `src/types.ts`, append to `AgentChatEventType` (before the terminating `;`):

```typescript
  | "turn.summary"
  | "loop.result";
```

- [ ] **Step 2: Add the message metadata field**

In `AgentChatMessage`, add after `turnSummary?`:

```typescript
  /** Present when this message was produced by a scheduled loop run
   * surfaced inline on the parent conversation. */
  loopResult?: {
    runSessionId?: string;
    scheduledSessionId?: string;
    runCompletedAt?: string;
  };
```

- [ ] **Step 3: Register the listened event**

In `src/runtime/events.ts`, add `"loop.result"` to `AGENT_CHAT_LISTENED_EVENTS` (after `"turn.summary"`):

```typescript
  "turn.summary",
  "loop.result",
```

- [ ] **Step 4: Typecheck**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run typecheck` (or the SDK's `tsc --noEmit`).
Expected: no new type errors.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/types.ts sdk/agent-chat-react/src/runtime/events.ts
git commit -m "feat(agent-chat): register loop.result event type"
```

---

## Task 6: SDK reducer case for `loop.result`

**Files:**
- Modify: `sdk/agent-chat-react/src/runtime/reducer.ts` (add case near `case "skill.invoked":`)
- Test: `sdk/agent-chat-react/src/runtime/reducer.test.ts` (create if absent, else append)

**Interfaces:**
- Consumes: reducer `(state, event) -> state`, `withMessages`, `AgentChatMessage` (with `loopResult`).
- Produces: a completed assistant-role message appended for a `loop.result` event, `isRunning` untouched.

- [ ] **Step 1: Write the failing test**

In the reducer test file:

```typescript
import { describe, it, expect } from "vitest";
import { agentChatReducer, initialAgentChatState } from "./reducer";

describe("loop.result", () => {
  it("appends a completed assistant message and does not set isRunning", () => {
    const state = { ...initialAgentChatState(), isRunning: false };
    const next = agentChatReducer(state, {
      type: "loop.result",
      eventId: 42,
      data: {
        content: "Weather in Bucharest: 22C",
        run_session_id: "r1",
        scheduled_session_id: "s1",
        run_completed_at: "2026-07-03T05:01:00Z",
      },
    });
    const msg = next.messages[next.messages.length - 1];
    expect(msg.role).toBe("assistant");
    expect(msg.content).toBe("Weather in Bucharest: 22C");
    expect(msg.status).toBe("complete");
    expect(msg.loopResult?.runSessionId).toBe("r1");
    expect(next.isRunning).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates/sdk/agent-chat-react && npx vitest run src/runtime/reducer.test.ts`
Expected: FAIL — no assistant message appended (unhandled type returns state unchanged).

- [ ] **Step 3: Implement the case**

In `reducer.ts`, add before `case "artifact.created":`:

```typescript
    case "loop.result":
      return withMessages(nextState, [
        ...nextState.messages,
        {
          id: `evt-${event.eventId}`,
          role: "assistant",
          content: stringValue(event.data.content),
          createdAt: new Date(),
          status: "complete",
          loopResult: {
            runSessionId: stringValue(event.data.run_session_id) || undefined,
            scheduledSessionId:
              stringValue(event.data.scheduled_session_id) || undefined,
            runCompletedAt: stringValue(event.data.run_completed_at) || undefined,
          },
        },
      ]);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npx vitest run src/runtime/reducer.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/runtime/reducer.ts sdk/agent-chat-react/src/runtime/reducer.test.ts
git commit -m "feat(agent-chat): reduce loop.result into an assistant message"
```

---

## Task 7: SDK thread affordance

**Files:**
- Modify: `sdk/agent-chat-react/src/components/chat/chat-thread.tsx`

**Interfaces:**
- Consumes: `AgentChatMessage.loopResult`.

- [ ] **Step 1: Render the affordance**

Find where an assistant message bubble is rendered in `chat-thread.tsx`. Add a
small metadata line, shown only when `message.loopResult` is set. Example
(adapt to the file's existing JSX/classnames):

```tsx
{message.loopResult && (
  <div className="text-xs text-muted-foreground/70 mb-1">
    From loop
    {message.loopResult.runCompletedAt
      ? ` · ${new Date(message.loopResult.runCompletedAt).toLocaleTimeString()}`
      : ""}
  </div>
)}
```

- [ ] **Step 2: Typecheck + build**

Run: `cd /work/surogates/sdk/agent-chat-react && npm run typecheck && npm run build`
Expected: clean.

- [ ] **Step 3: Manual verification note**

The affordance is visual; verify in a host app after Task 8/10 wiring. No unit
test required for the JSX label.

- [ ] **Step 4: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/chat/chat-thread.tsx
git commit -m "feat(agent-chat): show 'From loop' affordance on loop.result messages"
```

---

## Task 8: Web host raw event type

**Files:**
- Modify: `web/src/types/session.ts`

**Interfaces:**
- Consumes: raw session event type union. The chat adapter opens the raw SSE
  stream and delegates filtering to the SDK runtime (no adapter change needed).

- [ ] **Step 1: Add the type**

In `web/src/types/session.ts`, add `"loop.result"` wherever the raw session
event `type` union / enum is declared (mirror how `"llm.response"` /
`"inbox.task_complete"` appear).

- [ ] **Step 2: Typecheck**

Run: `cd /work/surogates/web && npm run typecheck`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add web/src/types/session.ts
git commit -m "feat(web): accept loop.result raw session event"
```

---

## Task 9: Ops observability view

**Files:**
- Modify: `surogates/db/observability.sql` (`v_session_messages`, ~line 660)

**Interfaces:**
- Produces: `loop.result` rows appear in `v_session_messages`.

- [ ] **Step 1: Add the type to the view whitelist**

In the `WHERE e.type IN (...)` list of `v_session_messages`, add `'loop.result'`:

```sql
    'user.feedback',
    'loop.result'
);
```

- [ ] **Step 2: Verify the SQL is well-formed**

Run (against the PROD-style test DB or a local psql): apply the `CREATE OR
REPLACE VIEW v_session_messages` statement and confirm no syntax error, e.g.:

```bash
# local dev DB only
psql "$SUROGATES_DATABASE_URL" -c "$(sed -n '/CREATE OR REPLACE VIEW v_session_messages/,/);/p' surogates/db/observability.sql)"
psql "$SUROGATES_DATABASE_URL" -c "SELECT 1 FROM v_session_messages LIMIT 1;"
```

Expected: `CREATE VIEW` then a clean select (0+ rows).

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add surogates/db/observability.sql
git commit -m "feat(observability): surface loop.result in v_session_messages"
```

---

## Task 10: Ops frontend thread rendering

**Files:**
- Modify: `/work/surogate-ops/frontend/src/types/session.ts` (`EventType` object, ~line 28)
- Modify: `/work/surogate-ops/frontend/src/components/sessions/session-thread.tsx` (~line 200)

**Interfaces:**
- Consumes: `EventType.LOOP_RESULT`; the `AgentTurn` component (reads message content).

- [ ] **Step 1: Add the enum entry**

In `frontend/src/types/session.ts`, add to the `EventType` object:

```typescript
  LOOP_RESULT: "loop.result",
```

- [ ] **Step 2: Include loop.result in the turn filter and render as an agent turn**

In `session-thread.tsx`, extend the `turns` filter and render:

```tsx
  const turns = useMemo(
    () =>
      messages.filter(
        (m) =>
          m.type === EventType.USER_MESSAGE ||
          m.type === EventType.LLM_RESPONSE ||
          m.type === EventType.LOOP_RESULT,
      ),
    [messages],
  );
```

and in the map, route `LOOP_RESULT` through `AgentTurn`:

```tsx
      {turns.map((m) =>
        m.type === EventType.USER_MESSAGE ? (
          <UserTurn key={m.eventId} msg={m} />
        ) : (
          <AgentTurn
            key={m.eventId}
            msg={m}
            agentLabel={agentLabel}
            feedback={feedbackMap.get(m.eventId)}
          />
        ),
      )}
```

- [ ] **Step 3: Ensure AgentTurn reads loop.result content**

Inspect `AgentTurn`: if it extracts content from `data.message.content`
(llm.response shape), add a fallback to `data.content` so a `loop.result`
event renders its text. Keep it minimal:

```tsx
  const text =
    msg.data?.message?.content ?? msg.data?.content ?? "";
```

- [ ] **Step 4: Typecheck**

Run: `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/types/session.ts frontend/src/components/sessions/session-thread.tsx
git commit -m "feat(sessions): render loop.result as an agent turn in the thread"
```

---

## Final verification

- [ ] **Backend suite**

Run: `/work/surogates/.venv/bin/pytest tests/test_loop_result_delivery.py tests/test_loop_result_replay_safety.py tests/test_channel_delivery.py -q`
Expected: all pass.

- [ ] **SDK suite + build**

Run: `cd /work/surogates/sdk/agent-chat-react && npx vitest run && npm run build`
Expected: pass + clean build.

- [ ] **Hosts typecheck**

Run: `cd /work/surogates/web && npm run typecheck` and `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: clean.

- [ ] **End-to-end (staging/local)**

Create a `/loop … every 1 minute` from a web session; after the next tick,
confirm the run's answer appears inline in that conversation and that no
`inbox.task_complete` card is created for the run (query `inbox_items` for the
run session id → 0 rows).
