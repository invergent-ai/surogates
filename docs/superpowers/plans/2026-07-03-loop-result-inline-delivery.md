# Loop-result Inline Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface each web/api-originated loop run's final answer inline in the originating conversation with a parent-session `loop.result` event, without waking the parent agent or adding the result to replay context.

**Architecture:** The harness adds a new `loop.result` event type and emits it on a scheduled run's web/api parent during `_complete_session`. The child run still records its own `session.complete`, but web/api scheduled runs skip the redundant `inbox.task_complete` card and advance the child cursor to the child terminal event. The shared React SDK listens for `loop.result` and renders it as assistant output; ops includes the event in `v_session_messages` and both session-thread renderers.

**Tech Stack:** Python 3.12 async harness, SQLAlchemy-backed session store, pytest, PostgreSQL SQL view, TypeScript/React, Vitest, Vite/tsc.

---

## Progress

- [x] Task 1: Backend event type + replay/outbox guards
- [x] Task 2: Backend parent-resolution helper
- [x] Task 3: Backend inline emission + inbox suppression + cursor fallback
- [x] Task 4: SDK event registration + reducer
- [x] Task 5: SDK chat-thread affordance
- [x] Task 6: Surogates web raw event type
- [x] Task 7: Ops observability view
- [ ] Task 8: Ops frontend thread rendering
- [ ] Task 9: Final verification

---

## Scope And Repos

- Spec: `/work/surogates/docs/superpowers/specs/2026-07-03-loop-result-inline-delivery-design.md`.
- Main repo: `/work/surogates`.
- Ops repo: `/work/surogate-ops`.
- Backend tests run with `/work/surogates/.venv/bin/pytest`.
- Frontend commands: use `pnpm` for `/work/surogates/sdk/agent-chat-react` (root `/work/surogates/pnpm-lock.yaml`), and use `npm` for `/work/surogates/web` and `/work/surogate-ops/frontend` (both have `package-lock.json`).
- Do not add `loop.result` to `SessionStore._DELIVERABLE_EVENTS`.
- Do not add `loop.result` to `ContextReplayMixin._rebuild_messages`.
- Do not enqueue or otherwise wake the parent session.
- Commit `/work/surogates` and `/work/surogate-ops` changes separately.

## File Structure

Backend in `/work/surogates`:

- Modify `surogates/session/events.py`: add `EventType.LOOP_RESULT`.
- Modify `surogates/harness/loop_artifact_completion.py`: add helper and completion-path surfacing.
- Test `tests/test_loop_result_inline_delivery.py`: backend event type, gating, emission, suppression, cursor fallback, best-effort behavior.
- Test `tests/test_loop_result_replay_safety.py`: replay/outbox guard.

SDK and web host in `/work/surogates`:

- Modify `sdk/agent-chat-react/src/types.ts`: add `"loop.result"` and `AgentChatMessage.loopResult`.
- Modify `sdk/agent-chat-react/src/runtime/events.ts`: listen for `"loop.result"`.
- Modify `sdk/agent-chat-react/src/runtime/reducer.ts`: append completed assistant message for `loop.result`.
- Modify `sdk/agent-chat-react/src/components/chat/chat-thread.tsx`: show a compact "From loop" affordance.
- Test `sdk/agent-chat-react/tests/listened-events.test.ts`: listened-event registration.
- Test `sdk/agent-chat-react/tests/reducer.test.ts`: reducer behavior.
- Modify `web/src/types/session.ts`: accept raw `"loop.result"` session events.

Ops/admin:

- Modify `/work/surogates/surogates/db/observability.sql`: include `loop.result` in `v_session_messages`.
- Modify `/work/surogate-ops/frontend/src/types/session.ts`: add `EventType.LOOP_RESULT`.
- Modify `/work/surogate-ops/frontend/src/components/sessions/session-thread.tsx`: render loop result in the compact thread.
- Modify `/work/surogate-ops/frontend/src/components/sessions/thread-tab.tsx`: render loop result in the detailed thread.

---

## Task 1: Backend Event Type And Replay Guards

**Files:**
- Modify: `/work/surogates/surogates/session/events.py`
- Create: `/work/surogates/tests/test_loop_result_replay_safety.py`

- [ ] **Step 1: Write the failing guard tests**

Create `/work/surogates/tests/test_loop_result_replay_safety.py`:

```python
from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.session.events import EventType
from surogates.session.store import _DELIVERABLE_EVENTS


def _event(event_type: EventType, data: dict, event_id: int = 1):
    return SimpleNamespace(
        id=event_id,
        session_id=uuid4(),
        type=event_type.value,
        data=data,
    )


def test_loop_result_event_type_exists():
    assert EventType.LOOP_RESULT.value == "loop.result"


def test_loop_result_is_not_a_deliverable_event():
    assert EventType.LOOP_RESULT not in _DELIVERABLE_EVENTS


def test_rebuild_messages_ignores_loop_result():
    host = type("_ReplayHost", (ContextReplayMixin,), {})()
    rebuilt = host._rebuild_messages([
        _event(
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": "normal reply"}},
            1,
        ),
        _event(EventType.LOOP_RESULT, {"content": "scheduled output"}, 2),
    ])

    joined = "\n".join(str(m.get("content", "")) for m in rebuilt)
    assert "normal reply" in joined
    assert "scheduled output" not in joined
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_replay_safety.py -q
```

Expected: fails with `AttributeError: LOOP_RESULT`.

- [ ] **Step 3: Add the event type**

In `/work/surogates/surogates/session/events.py`, add this immediately after the inbox event block:

```python
    # Scheduled loop run result surfaced inline on a web/api parent session.
    LOOP_RESULT = "loop.result"
```

- [ ] **Step 4: Run the passing guard tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_replay_safety.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/session/events.py tests/test_loop_result_replay_safety.py
git commit -m "feat(events): add loop.result event type"
```

---

## Task 2: Backend Parent Resolution Helper

**Files:**
- Modify: `/work/surogates/surogates/harness/loop_artifact_completion.py`
- Create/modify: `/work/surogates/tests/test_loop_result_inline_delivery.py`

- [ ] **Step 1: Write failing gating tests**

Create `/work/surogates/tests/test_loop_result_inline_delivery.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.session.store import SessionNotFoundError


def _session(*, channel: str, parent_id=None, config=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-a",
        channel=channel,
        status="active",
        parent_id=parent_id,
        title="Loop run",
        config=config or {},
        created_at=now,
        updated_at=now,
        task_id=None,
    )


class _ParentStore:
    def __init__(self, parent=None):
        self.parent = parent

    async def get_session(self, session_id):
        if self.parent is not None and session_id == self.parent.id:
            return self.parent
        raise SessionNotFoundError(str(session_id))


def _harness(store):
    host = type("_Harness", (ArtifactCompletionMixin,), {})()
    host._store = store
    return host


@pytest.mark.parametrize("parent_channel", ["web", "api"])
async def test_resolves_web_and_api_loop_parent(parent_channel):
    parent = _session(channel=parent_channel)
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is parent


@pytest.mark.parametrize("parent_channel", ["slack", "telegram", "teams", "ambient"])
async def test_skips_channel_and_private_parents(parent_channel):
    parent = _session(channel=parent_channel)
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is None


async def test_skips_detached_scheduled_run():
    child = _session(
        channel="scheduled",
        parent_id=None,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore())._resolve_loop_result_parent(child) is None


async def test_skips_missing_parent():
    child = _session(
        channel="scheduled",
        parent_id=uuid4(),
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore())._resolve_loop_result_parent(child) is None


async def test_accepts_legacy_scheduled_run_marker_even_if_channel_drifted():
    parent = _session(channel="web")
    child = _session(
        channel="api",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )

    assert await _harness(_ParentStore(parent))._resolve_loop_result_parent(child) is parent
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_inline_delivery.py -q
```

Expected: fails with `AttributeError: '_Harness' object has no attribute '_resolve_loop_result_parent'`.

- [ ] **Step 3: Implement the helper**

In `/work/surogates/surogates/harness/loop_artifact_completion.py`, add this method inside `ArtifactCompletionMixin`, before `_complete_session`:

```python
    async def _resolve_loop_result_parent(self, session: Session) -> Session | None:
        """Return the web/api parent that should receive this loop run result."""
        config = session.config or {}
        is_scheduled_run = (
            session.channel == "scheduled"
            or bool(config.get("scheduled_session_id"))
        )
        if not is_scheduled_run or session.parent_id is None:
            return None

        from surogates.session.store import SessionNotFoundError

        try:
            parent = await self._store.get_session(session.parent_id)
        except SessionNotFoundError:
            return None

        if parent.channel not in {"web", "api"}:
            return None
        return parent
```

- [ ] **Step 4: Run the passing tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_inline_delivery.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_artifact_completion.py tests/test_loop_result_inline_delivery.py
git commit -m "feat(harness): resolve loop-result parent sessions"
```

---

## Task 3: Backend Inline Emission And Inbox Suppression

**Files:**
- Modify: `/work/surogates/surogates/harness/loop_artifact_completion.py`
- Modify: `/work/surogates/tests/test_loop_result_inline_delivery.py`

- [ ] **Step 1: Add failing completion-path tests**

Append to `/work/surogates/tests/test_loop_result_inline_delivery.py`:

```python
from surogates.session.events import EventType


def _llm_response(content: str):
    return SimpleNamespace(
        type=EventType.LLM_RESPONSE.value,
        data={"message": {"role": "assistant", "content": content}},
    )


class _RecordingStore(_ParentStore):
    def __init__(self, parent=None, child_events=None, fail_loop_result=False):
        super().__init__(parent)
        self.child_events = list(child_events or [])
        self.fail_loop_result = fail_loop_result
        self.emitted = []
        self.status_updates = []
        self.cursor_advancements = []
        self.next_event_id = 100

    async def get_events(self, session_id):
        return list(self.child_events)

    async def emit_event(self, session_id, event_type, data):
        if event_type == EventType.LOOP_RESULT and self.fail_loop_result:
            raise RuntimeError("boom")
        self.next_event_id += 1
        self.emitted.append((self.next_event_id, session_id, event_type, data))
        return self.next_event_id

    async def update_session_status(self, session_id, status):
        self.status_updates.append((session_id, status))

    async def advance_harness_cursor(self, session_id, cursor, lease_token):
        self.cursor_advancements.append((session_id, cursor, lease_token))


def _completion_harness(store):
    host = _harness(store)
    host._worker_id = "worker-1"
    host._sandbox_pool = None
    host._memory_manager = None
    host._turn_summarizer = None
    host._redis = None
    host._session_factory = None
    return host


async def _complete(host, child, *, messages=None, reason="stop", through_event_id=None):
    await host._complete_session(
        child,
        messages=messages if messages is not None else [{"role": "assistant", "content": "done"}],
        lease=SimpleNamespace(lease_token="lease-1"),
        reason=reason,
        through_event_id=through_event_id,
    )


def _types(store):
    return [event_type for _, _, event_type, _ in store.emitted]


async def test_web_parent_gets_loop_result_and_inbox_is_suppressed():
    schedule_id = uuid4()
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(schedule_id)},
    )
    store = _RecordingStore(parent, [_llm_response("Full final answer")])

    await _complete(_completion_harness(store), child)

    loop_rows = [row for row in store.emitted if row[2] == EventType.LOOP_RESULT]
    assert len(loop_rows) == 1
    _, emitted_session_id, _, payload = loop_rows[0]
    assert emitted_session_id == parent.id
    assert payload["run_session_id"] == str(child.id)
    assert payload["scheduled_session_id"] == str(schedule_id)
    assert payload["content"] == "Full final answer"
    assert payload["outcome"] == "success"
    assert isinstance(payload["duration_seconds"], int)
    assert payload["run_completed_at"]
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)


async def test_api_parent_gets_loop_result_and_inbox_is_suppressed():
    parent = _session(channel="api")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("API final answer")])

    await _complete(_completion_harness(store), child)

    assert EventType.LOOP_RESULT in _types(store)
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)


async def test_channel_parent_keeps_existing_inbox_completion_and_no_loop_result():
    parent = _session(channel="slack")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Channel final answer")])

    await _complete(_completion_harness(store), child)

    assert EventType.LOOP_RESULT not in _types(store)
    assert EventType.INBOX_TASK_COMPLETE in _types(store)


async def test_empty_final_response_suppresses_inbox_but_emits_no_loop_result():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, child_events=[])

    await _complete(_completion_harness(store), child, messages=[])

    assert EventType.LOOP_RESULT not in _types(store)
    assert EventType.INBOX_TASK_COMPLETE not in _types(store)
    assert store.status_updates == [(child.id, "completed")]


async def test_loop_result_failure_does_not_abort_completion_and_still_suppresses_inbox():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")], fail_loop_result=True)

    await _complete(_completion_harness(store), child)

    assert EventType.INBOX_TASK_COMPLETE not in _types(store)
    assert store.status_updates == [(child.id, "completed")]
    assert store.cursor_advancements


async def test_cursor_uses_child_session_complete_when_inbox_is_suppressed():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")])

    await _complete(_completion_harness(store), child)

    session_complete_id = next(
        event_id for event_id, _, event_type, _ in store.emitted
        if event_type == EventType.SESSION_COMPLETE
    )
    assert store.cursor_advancements == [(child.id, session_complete_id, "lease-1")]


async def test_explicit_through_event_id_still_wins_for_cursor():
    parent = _session(channel="web")
    child = _session(
        channel="scheduled",
        parent_id=parent.id,
        config={"scheduled_session_id": str(uuid4())},
    )
    store = _RecordingStore(parent, [_llm_response("Final")])

    await _complete(_completion_harness(store), child, through_event_id=77)

    assert store.cursor_advancements == [(child.id, 77, "lease-1")]
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_inline_delivery.py -q
```

Expected: failures showing `loop.result` is not emitted and web/api inbox cards are still emitted.

- [ ] **Step 3: Import final-response extraction**

In `/work/surogates/surogates/harness/loop_artifact_completion.py`, add:

```python
from surogates.harness.message_utils import extract_final_response
```

The file already imports `datetime`, `timezone`, `_seconds_since`, and `_last_assistant_message_excerpt`.

- [ ] **Step 4: Replace the completion event block**

In `_complete_session`, replace the current `SESSION_COMPLETE` plus unconditional `INBOX_TASK_COMPLETE` block with:

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

        loop_result_parent = None
        try:
            loop_result_parent = await self._resolve_loop_result_parent(session)
        except Exception:
            logger.debug(
                "Failed to resolve loop.result parent for %s",
                session.id,
                exc_info=True,
            )

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
                                (session.config or {}).get("scheduled_session_id") or ""
                            ),
                            "content": content,
                            "outcome": outcome,
                            "duration_seconds": _seconds_since(session.created_at),
                            "run_completed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
            except Exception:
                logger.warning(
                    "Failed to emit loop.result on parent %s for run %s",
                    loop_result_parent.id,
                    session.id,
                    exc_info=True,
                )

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

- [ ] **Step 5: Replace the cursor fallback**

In `_complete_session`, replace the existing `cursor_target` assignment with:

```python
        cursor_target = (
            through_event_id
            if through_event_id is not None
            else (
                inbox_event_id
                if inbox_event_id is not None
                else session_complete_event_id
            )
        )
```

- [ ] **Step 6: Run the passing backend tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest tests/test_loop_result_inline_delivery.py tests/test_loop_result_replay_safety.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/harness/loop_artifact_completion.py tests/test_loop_result_inline_delivery.py
git commit -m "feat(harness): surface web api loop results inline"
```

---

## Task 4: SDK Event Registration And Reducer

**Files:**
- Modify: `/work/surogates/sdk/agent-chat-react/src/types.ts`
- Modify: `/work/surogates/sdk/agent-chat-react/src/runtime/events.ts`
- Modify: `/work/surogates/sdk/agent-chat-react/src/runtime/reducer.ts`
- Modify: `/work/surogates/sdk/agent-chat-react/tests/listened-events.test.ts`
- Modify: `/work/surogates/sdk/agent-chat-react/tests/reducer.test.ts`

- [ ] **Step 1: Add failing SDK tests**

Add this test inside the existing `describe("AGENT_CHAT_LISTENED_EVENTS", ...)` block in `/work/surogates/sdk/agent-chat-react/tests/listened-events.test.ts`:

```typescript
  it("includes loop.result so scheduled results reach the reducer", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toContain("loop.result");
  });
```

Append inside the existing `describe("applyAgentChatEvent", ...)` block in `/work/surogates/sdk/agent-chat-react/tests/reducer.test.ts`:

```typescript
  it("appends loop.result as completed assistant output", () => {
    const running = {
      ...createInitialAgentChatState(),
      isRunning: false,
    };

    const next = applyAgentChatEvent(running, {
      type: "loop.result",
      eventId: 42,
      data: {
        content: "Loop says: done.",
        run_session_id: "run-1",
        scheduled_session_id: "schedule-1",
        run_completed_at: "2026-07-03T12:00:00Z",
      },
    });

    expect(next.isRunning).toBe(false);
    expect(next.lastEventId).toBe(42);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      id: "evt-42",
      role: "assistant",
      content: "Loop says: done.",
      status: "complete",
      loopResult: {
        runSessionId: "run-1",
        scheduledSessionId: "schedule-1",
        runCompletedAt: "2026-07-03T12:00:00Z",
      },
    });
  });
```

- [ ] **Step 2: Run the failing SDK tests**

Run:

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm test tests/listened-events.test.ts tests/reducer.test.ts
```

Expected: type/test failure because `"loop.result"` is not in `AgentChatEventType` and reducer has no case.

- [ ] **Step 3: Add SDK types**

In `/work/surogates/sdk/agent-chat-react/src/types.ts`, add the metadata field to `AgentChatMessage` after `turnSummary?: AgentChatTurnSummary;`:

```typescript
  loopResult?: {
    runSessionId?: string;
    scheduledSessionId?: string;
    runCompletedAt?: string;
  };
```

In the `AgentChatEventType` union, add:

```typescript
  | "turn.summary"
  | "loop.result";
```

- [ ] **Step 4: Listen for the event**

In `/work/surogates/sdk/agent-chat-react/src/runtime/events.ts`, add `"loop.result"` after `"turn.summary"`:

```typescript
  "turn.summary",
  "loop.result",
```

- [ ] **Step 5: Add the reducer case**

In `/work/surogates/sdk/agent-chat-react/src/runtime/reducer.ts`, add this `switch` case after `case "skill.invoked":` and before artifact cases:

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

Do not change token usage and do not set `isRunning`.

- [ ] **Step 6: Run the passing SDK tests**

Run:

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm test tests/listened-events.test.ts tests/reducer.test.ts
pnpm run typecheck
```

Expected: tests and typecheck pass.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/types.ts \
  sdk/agent-chat-react/src/runtime/events.ts \
  sdk/agent-chat-react/src/runtime/reducer.ts \
  sdk/agent-chat-react/tests/listened-events.test.ts \
  sdk/agent-chat-react/tests/reducer.test.ts
git commit -m "feat(agent-chat): handle loop.result messages"
```

---

## Task 5: SDK Chat Thread Affordance

**Files:**
- Modify: `/work/surogates/sdk/agent-chat-react/src/components/chat/chat-thread.tsx`

- [ ] **Step 1: Add a loop affordance helper**

In `/work/surogates/sdk/agent-chat-react/src/components/chat/chat-thread.tsx`, add this helper near the small render helpers before `SimpleAssistantGroup`:

```tsx
function LoopResultAffordance({ message }: { message?: ChatMessageType }) {
  if (!message?.loopResult) return null;
  const completedAt = message.loopResult.runCompletedAt
    ? new Date(message.loopResult.runCompletedAt)
    : null;
  const time =
    completedAt && !Number.isNaN(completedAt.getTime())
      ? completedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "";
  return (
    <div className="text-xs text-muted-foreground/70">
      From loop{time ? ` · ${time}` : ""}
    </div>
  );
}
```

- [ ] **Step 2: Render it in Simple mode**

In `SimpleAssistantGroup`, inside the `finalText && (...)` block, replace:

```tsx
        {finalText && (
          <SimpleFinalAnswer
            text={finalText}
            isStreaming={tail?.status === "streaming"}
            tail={tail}
          />
        )}
```

with:

```tsx
        {finalText && (
          <div className="space-y-1">
            <LoopResultAffordance message={tail} />
            <SimpleFinalAnswer
              text={finalText}
              isStreaming={tail?.status === "streaming"}
              tail={tail}
            />
          </div>
        )}
```

- [ ] **Step 3: Render it in Expert mode**

In `TextEntry` (the function that renders `Extract<TimelineEntry, { kind: "text" }>`), render the same helper immediately before the `MessageResponse` that displays `entry.content`:

```tsx
      <LoopResultAffordance message={entry.msg} />
```

Keep the existing `MessageResponse` and `TurnFeedback` logic intact.

- [ ] **Step 4: Typecheck and build**

Run:

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm run typecheck
pnpm run build
```

Expected: typecheck and build pass.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/chat/chat-thread.tsx
git commit -m "feat(agent-chat): label loop result messages"
```

---

## Task 6: Surogates Web Raw Event Type

**Files:**
- Modify: `/work/surogates/web/src/types/session.ts`

- [ ] **Step 1: Add the type**

In `/work/surogates/web/src/types/session.ts`, add `"loop.result"` to the `EventType` union after `"llm.response"`:

```typescript
  | "llm.response"
  | "loop.result"
```

- [ ] **Step 2: Typecheck web**

Run:

```bash
cd /work/surogates/web
npm run typecheck
```

Expected: typecheck passes.

- [ ] **Step 3: Commit**

```bash
cd /work/surogates
git add web/src/types/session.ts
git commit -m "feat(web): accept loop.result session events"
```

---

## Task 7: Ops Observability View

**Files:**
- Modify: `/work/surogates/surogates/db/observability.sql`

- [ ] **Step 1: Add `loop.result` to the SQL view**

In `/work/surogates/surogates/db/observability.sql`, update the `v_session_messages` `WHERE e.type IN (...)` list:

```sql
    'expert.override',
    'user.feedback',
    'loop.result'
);
```

- [ ] **Step 2: Commit the SQL in `/work/surogates`**

```bash
cd /work/surogates
git add surogates/db/observability.sql
git commit -m "feat(observability): include loop.result in session messages"
```

Do not include `/work/surogate-ops` files in this commit.

---

## Task 8: Ops Frontend Thread Rendering

**Files:**
- Modify: `/work/surogate-ops/frontend/src/components/sessions/session-thread.tsx`
- Modify: `/work/surogate-ops/frontend/src/components/sessions/thread-tab.tsx`
- Modify: `/work/surogate-ops/frontend/src/types/session.ts`

- [ ] **Step 1: Add the ops event constant**

In `/work/surogate-ops/frontend/src/types/session.ts`, add to the `EventType` object after `LLM_RESPONSE`:

```typescript
  LOOP_RESULT: "loop.result",
```

- [ ] **Step 2: Update compact `SessionThread` content extraction**

In `/work/surogate-ops/frontend/src/components/sessions/session-thread.tsx`, replace the `content` line in `AgentTurn`:

```tsx
  const content = message?.content ?? "";
```

with:

```tsx
  const content = message?.content ?? asStr(d?.content) ?? "";
```

- [ ] **Step 3: Include `loop.result` in the compact turn filter**

In the same file, update the `turns` filter:

```tsx
          m.type === EventType.USER_MESSAGE ||
          m.type === EventType.LLM_RESPONSE ||
          m.type === EventType.LOOP_RESULT,
```

The existing map already sends all non-user turns through `AgentTurn`, so no additional branch is needed.

- [ ] **Step 4: Add a detailed-thread bubble**

In `/work/surogate-ops/frontend/src/components/sessions/thread-tab.tsx`, add this function after `AssistantBubble`:

```tsx
function LoopResultBubble({ msg }: { msg: SessionMessage }) {
  const d = asObj(msg.data);
  const content = asStr(d?.content) ?? "";
  const completedAt = asStr(d?.run_completed_at);
  return (
    <div className="flex gap-2.5">
      <Avatar letter="L" color="#3B82F6" />
      <Bubble side="left">
        <div className="mb-1 text-[9px] font-semibold uppercase text-muted-foreground/60">
          From loop{completedAt ? ` · ${formatTimestamp(completedAt)}` : ""}
        </div>
        <div className="text-[12px] text-foreground leading-relaxed whitespace-pre-wrap wrap-break-word">
          {content || (
            <span className="text-muted-foreground/30 italic">empty</span>
          )}
        </div>
        <TimestampFooter createdAt={msg.createdAt} />
      </Bubble>
    </div>
  );
}
```

- [ ] **Step 5: Render `loop.result` in detailed `ThreadTab`**

In the `switch (m.type)` in `ThreadTab`, add:

```tsx
          case EventType.LOOP_RESULT:
            return <LoopResultBubble key={m.eventId} msg={m} />;
```

Place it next to `EventType.LLM_RESPONSE`.

- [ ] **Step 6: Typecheck ops**

Run:

```bash
cd /work/surogate-ops/frontend
npm run typecheck
```

Expected: typecheck passes.

- [ ] **Step 7: Commit ops changes**

```bash
cd /work/surogate-ops
git add frontend/src/types/session.ts \
  frontend/src/components/sessions/session-thread.tsx \
  frontend/src/components/sessions/thread-tab.tsx
git commit -m "feat(sessions): render loop results in session threads"
```

---

## Task 9: Final Verification

**Files:**
- No code edits unless verification exposes a defect.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
cd /work/surogates
/work/surogates/.venv/bin/pytest \
  tests/test_loop_result_inline_delivery.py \
  tests/test_loop_result_replay_safety.py \
  tests/test_channel_delivery.py \
  -q
```

Expected: all pass. `tests/test_channel_delivery.py` confirms existing channel-loop outbox behavior remains intact.

- [ ] **Step 2: Run SDK tests and build**

Run:

```bash
cd /work/surogates/sdk/agent-chat-react
pnpm test tests/listened-events.test.ts tests/reducer.test.ts
pnpm run typecheck
pnpm run build
```

Expected: tests, typecheck, and build pass.

- [ ] **Step 3: Run host typechecks**

Run:

```bash
cd /work/surogates/web
npm run typecheck

cd /work/surogate-ops/frontend
npm run typecheck
```

Expected: both pass.

- [ ] **Step 4: Inspect git state**

Run:

```bash
git -C /work/surogates status --short
git -C /work/surogate-ops status --short
```

Expected: only intended files are modified, or no changes if every task was committed. Do not revert unrelated pre-existing files.

- [ ] **Step 5: Manual staging check**

In a local or staging web session:

1. Create a `/loop` that runs quickly.
2. Wait for the next scheduled run to complete.
3. Confirm the parent conversation displays the run's final answer inline with the "From loop" affordance.
4. Query `events` for the parent session and confirm one `loop.result` row with `run_session_id`, `scheduled_session_id`, and full `content`.
5. Query `inbox_items` for the child run's `inbox.task_complete` source event and confirm no new web/api scheduled-run completion card was created.
6. Create or inspect a Slack/Telegram scheduled run and confirm channel delivery still uses the existing outbox path, with no parent `loop.result`.
