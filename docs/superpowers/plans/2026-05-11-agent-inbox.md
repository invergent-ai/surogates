# Agent Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a per-agent, per-user inbox surfaced in the surogates web channel. The agent posts items when it needs user input, when a task completes, when a user-overridable policy denial fires, or on a long-running progress check-in. MVP delivers storage + the web pane with live updates; email/Slack/Telegram push are designed-for and deferred.

**Architecture:** A new `inbox_items` table in the surogates DB. `SessionStore.emit_event` writes to `inbox_items` in the same transaction as the event when the event type is in a new `_INBOX_EVENTS` set, gated on `session.user_id IS NOT NULL`. A post-commit Redis publish on `surogates:inbox:{user_id}` drives a new `/v1/inbox/stream` SSE endpoint. The web SPA gains an `/inbox` route with list + per-kind detail/action UI. A sweeper job expires pending items for terminal sessions.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async ORM (PostgreSQL via asyncpg), FastAPI + sse-starlette, Redis (pub/sub), pytest with `asyncio_mode = "auto"`, TypeScript/React SPA (Vite + TanStack Router), shadcn UI.

**Reference spec:** `docs/superpowers/specs/2026-05-11-agent-inbox-design.md`

## Phase 4 TODO

- [x] **Completed:** Task 16 - Add inbox sweeper for pending items on terminal sessions.

## Phase 5 TODO

- [x] **Completed:** Task 17 - Add inbox types and adapter API in `sdk/agent-chat-react`.
- [x] **Completed:** Task 18 - Add shared SDK inbox stream/unread hook.
- [x] **Completed:** Task 19 - Add shared SDK inbox list view.
- [x] **Completed:** Task 20 - Add shared SDK per-kind inbox detail views.
- [x] **Completed:** Task 21 - Add web auth-backed API client and `/inbox` route using SDK components.
- [x] **Completed:** Task 22 - Add Inbox sidebar item with live unread badge using SDK hook.

## Phase 6 TODO

- [x] **Completed:** Task 23 - Add clarify inbox end-to-end integration test.
- [ ] **In progress:** Task 24 - Add governance and completion inbox end-to-end integration tests.

## Phase 3 TODO

- [x] **Completed:** Task 10 - Add `SessionStore` helpers for the inbox API.
- [x] **Completed:** Task 11 - Add `GET /v1/inbox`.
- [x] **Completed:** Task 12 - Add inbox item detail, read, and ack routes.
- [x] **Completed:** Task 13 - Add governance decision response route.
- [x] **Completed:** Task 14 - Add inbox SSE stream.
- [x] **Completed:** Task 15 - Mark clarify inbox items responded from clarify responses.

## Phase 2 TODO

- [x] **Completed:** Task 5 - Emit `INBOX_INPUT_REQUIRED` from the clarify tool.
- [x] **Completed:** Task 6 - Emit `INBOX_TASK_COMPLETE` from `_complete_session`.
- [x] **Completed:** Task 7 - Add `overridable` metadata to `PolicyDecision`.
- [x] **Completed:** Task 8 - Emit `INBOX_GOVERNANCE_GATE` on overridable policy denial.
- [x] **Completed:** Task 9 - Emit `INBOX_PROGRESS_CHECKIN` at harness iteration checkpoints.

## Phase 1 TODO

- [x] **Completed:** Task 1 - Add four new `EventType` values.
- [x] **Completed:** Task 2 - Add `InboxItem` ORM model and indexes.
- [x] **Completed:** Task 3 - Build inbox payload parser.
- [x] **Completed:** Task 4 - Extend `SessionStore.emit_event` to mirror inbox events.

---

## Repository conventions to preserve

- Backend routers do not include `/v1` in their own prefix. `surogates/api/app.py` mounts them with `app.include_router(..., prefix="/v1", tags=[...])`; the inbox router can use `APIRouter(prefix="/inbox")`.
- Auth dependencies come from `surogates.tenant.auth.middleware`; use `get_current_tenant` and `TenantContext` from `surogates.tenant.context`.
- DB models are named `Session` and `Event` in `surogates/db/models.py`. `SessionRow` / `EventRow` are local aliases used only inside `surogates/session/store.py`.
- The work-queue helper is `surogates.config.enqueue_session(redis, agent_id, session_id)`.
- The web API layer uses `authFetch` from `web/src/api/auth.ts` and `/api/v1/...` URLs. There is no `web/src/api/client.ts`.
- TanStack routes use `createRoute` plus explicit registration in `web/src/app/router.tsx`; this repo is not using file-route generation.
- Clarify responses post `{ responses: ClarifyAnswer[] }` to `submitClarifyResponse`; they do not post `{ answers: ... }`.
- Inbox UI components must live in `sdk/agent-chat-react`; do not add them under `web/src` when implementing the UI phases.

## Phase 0 — Pre-flight

### Task 0: Establish baseline

**Files:**
- Read: `surogates/session/store.py:321-390` (existing `emit_event`)
- Read: `surogates/session/events.py` (existing `EventType` enum)
- Read: `surogates/db/models.py` (existing ORM model patterns)
- Read: `surogates/tests/conftest.py` (test fixtures)
- Read: `surogates/api/routes/clarify.py` (existing clarify response path)

- [ ] **Step 1: Confirm clean working tree**

Run: `git status`
Expected: clean tree on a feature branch (or worktree per superpowers:using-git-worktrees).

- [ ] **Step 2: Verify test suite is green at baseline**

Run: `pytest -x -q tests/ 2>&1 | tail -20`
Expected: all tests pass. If failures exist, fix or note them before proceeding — the plan's later test runs must be able to distinguish new failures from pre-existing ones.

- [ ] **Step 3: Read the spec once end-to-end**

Read: `docs/superpowers/specs/2026-05-11-agent-inbox-design.md`
Goal: hold the four kinds (`input_required`, `task_complete`, `governance_gate`, `progress_checkin`) and their payload shapes in head; remember the no-auto-replay decision for governance.

---

## Phase 1 — Schema, EventTypes, and emit_event extension

### Task 1: Add four new EventType values

**Files:**
- Modify: `surogates/session/events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_event_types.py`:

```python
"""Smoke test that the four inbox EventType values exist with the
documented values."""

from surogates.session.events import EventType


def test_inbox_event_types_exist_with_documented_values():
    assert EventType.INBOX_INPUT_REQUIRED.value == "inbox.input_required"
    assert EventType.INBOX_TASK_COMPLETE.value == "inbox.task_complete"
    assert EventType.INBOX_GOVERNANCE_GATE.value == "inbox.governance_gate"
    assert EventType.INBOX_PROGRESS_CHECKIN.value == "inbox.progress_checkin"
```

- [ ] **Step 2: Run test — verify it fails**

Run: `pytest tests/test_inbox_event_types.py -v`
Expected: FAIL — `AttributeError: INBOX_INPUT_REQUIRED`.

- [ ] **Step 3: Add the four values**

In `surogates/session/events.py`, add a new section before the closing of the `EventType` class (after the existing `BROWSER_*` block):

```python
    # Agent inbox — raised-hand moments mirrored into `inbox_items`.
    # Emitted alongside the existing event(s) that describe the same
    # moment in detail (TOOL_CALL for clarify, SESSION_COMPLETE for
    # task complete, POLICY_DENIED for an overridable governance gate,
    # nothing for progress check-ins). See
    # docs/superpowers/specs/2026-05-11-agent-inbox-design.md.
    INBOX_INPUT_REQUIRED = "inbox.input_required"
    INBOX_TASK_COMPLETE = "inbox.task_complete"
    INBOX_GOVERNANCE_GATE = "inbox.governance_gate"
    INBOX_PROGRESS_CHECKIN = "inbox.progress_checkin"
```

- [ ] **Step 4: Run test — verify it passes**

Run: `pytest tests/test_inbox_event_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/events.py tests/test_inbox_event_types.py
git commit -m "feat(inbox): add four inbox EventType values"
```

---

### Task 2: Add `InboxItem` ORM model

**Files:**
- Modify: `surogates/db/models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_model.py`:

```python
"""Schema-level tests for InboxItem."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from surogates.db.models import InboxItem


@pytest.mark.asyncio
async def test_inbox_item_can_be_inserted(db_session, sample_session, sample_event):
    item = InboxItem(
        org_id=sample_session.org_id,
        user_id=sample_session.user_id,
        session_id=sample_session.id,
        source_event_id=sample_event.id,
        kind="task_complete",
        title="Task complete",
        body="The work finished.",
        payload={"outcome": "success", "duration_seconds": 10},
    )
    db_session.add(item)
    await db_session.flush()
    assert item.id is not None
    assert item.status == "pending"
    assert item.read_at is None


@pytest.mark.asyncio
async def test_inbox_item_source_event_id_is_unique(
    db_session, sample_session, sample_event
):
    first = InboxItem(
        org_id=sample_session.org_id,
        user_id=sample_session.user_id,
        session_id=sample_session.id,
        source_event_id=sample_event.id,
        kind="task_complete",
        title="t",
    )
    second = InboxItem(
        org_id=sample_session.org_id,
        user_id=sample_session.user_id,
        session_id=sample_session.id,
        source_event_id=sample_event.id,  # duplicate
        kind="task_complete",
        title="t2",
    )
    db_session.add_all([first, second])
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

If `sample_session` / `sample_event` / `db_session` fixtures don't exist with those names, scan `tests/conftest.py` for the equivalents and use them. Names commonly used: `session_factory`, `event_factory`, `async_session`.

- [ ] **Step 2: Run tests — verify they fail**

Run: `pytest tests/test_inbox_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'InboxItem'`.

- [ ] **Step 3: Add the ORM model**

In `surogates/db/models.py`, after the existing event/session models and following the same `Mapped[...] = mapped_column(...)` style, add:

```python
class InboxItem(Base):
    """A raised-hand moment for the user.

    Mirrors a curated subset of `events` rows that the agent flagged as
    needing user attention. Written inside the same transaction as the
    underlying event via SessionStore.emit_event. See
    docs/superpowers/specs/2026-05-11-agent-inbox-design.md.
    """

    __tablename__ = "inbox_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    source_event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    action_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "idx_inbox_user_status_created",
            "user_id",
            "status",
            "created_at",
            postgresql_using="btree",
        ),
        Index("idx_inbox_org_created", "org_id", "created_at"),
        Index("idx_inbox_session", "session_id"),
    )
```

If `BigInteger`, `Text`, `JSONB`, `UUID`, `DateTime`, `ForeignKey`, `Index`, `func`, or `Mapped`/`mapped_column` aren't already imported in `db/models.py`, add them — match the existing imports' style.

- [ ] **Step 4: Run tests — verify they pass**

Run: `pytest tests/test_inbox_model.py -v`
Expected: PASS. If a fixture name doesn't exist, adjust the test to use the project's actual fixture names (read `tests/conftest.py`).

- [ ] **Step 5: Verify the table is created in test DB**

Run: `pytest tests/test_inbox_model.py -v --tb=short`
Expected: still PASS — the `Base.metadata.create_all` path in `surogates/db/engine.py:run_migrations` will pick up the new model on next bootstrap; tests run against an isolated DB whose fixtures call `create_all`.

- [ ] **Step 6: Commit**

```bash
git add surogates/db/models.py tests/test_inbox_model.py
git commit -m "feat(inbox): add InboxItem ORM model"
```

---

### Task 3: Build a payload-builder utility

**Files:**
- Create: `surogates/session/inbox_payload.py`
- Create: `tests/test_inbox_payload.py`

A single module turns raw event data into the `(kind, title, body, payload, action_ref)` tuple stored in `inbox_items`. Centralizing this keeps `emit_event` thin and gives one place to update truncation/derivation rules.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_payload.py`:

```python
"""Unit tests for the inbox payload builder."""

import pytest

from surogates.session.events import EventType
from surogates.session.inbox_payload import build_inbox_row, InboxRow


def test_input_required_row_includes_tool_call_id_and_questions():
    row = build_inbox_row(
        event_type=EventType.INBOX_INPUT_REQUIRED,
        event_data={
            "tool_call_id": "tc-1",
            "questions": [
                {"prompt": "Which color?"},
            ],
            "context": "Picking a primary color for the brand",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )
    assert isinstance(row, InboxRow)
    assert row.kind == "input_required"
    assert "Which color?" in row.title
    assert row.payload["tool_call_id"] == "tc-1"
    assert row.action_ref["type"] == "clarify_response"
    assert row.action_ref["tool_call_id"] == "tc-1"
    assert row.action_ref["endpoint"].endswith("/respond")


def test_task_complete_row_carries_outcome():
    row = build_inbox_row(
        event_type=EventType.INBOX_TASK_COMPLETE,
        event_data={
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 42,
            "session_title": "Refactor billing",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )
    assert row.kind == "task_complete"
    assert row.title == "Refactor billing"
    assert row.payload["outcome"] == "success"
    assert row.action_ref is None  # ack-only


def test_governance_gate_row_includes_tool_call_id():
    row = build_inbox_row(
        event_type=EventType.INBOX_GOVERNANCE_GATE,
        event_data={
            "tool_name": "send_email",
            "tool_call_id": "tc-7",
            "arguments_excerpt": "to=ceo@…",
            "deny_reason": "External recipient requires explicit approval.",
            "policy_id": "external-comms-v1",
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )
    assert row.kind == "governance_gate"
    assert "send_email" in row.title
    assert row.payload["tool_call_id"] == "tc-7"
    assert row.action_ref["choices"] == ["approve", "reject"]


def test_progress_checkin_row_is_ack_only():
    row = build_inbox_row(
        event_type=EventType.INBOX_PROGRESS_CHECKIN,
        event_data={
            "progress_summary": "Indexed 1,200 files.",
            "iterations": 14,
            "last_tool": "shell_exec",
            "elapsed_seconds": 1830,
        },
        session_id="00000000-0000-0000-0000-000000000001",
    )
    assert row.kind == "progress_checkin"
    assert "14" in row.title
    assert row.action_ref is None


def test_unknown_event_type_returns_none():
    row = build_inbox_row(
        event_type=EventType.LLM_RESPONSE,
        event_data={},
        session_id="00000000-0000-0000-0000-000000000001",
    )
    assert row is None
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `pytest tests/test_inbox_payload.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the builder**

Create `surogates/session/inbox_payload.py`:

```python
"""Derive `inbox_items` row fields from an inbox-class event.

`SessionStore.emit_event` calls `build_inbox_row` whenever an event type
is in `_INBOX_EVENTS`. The function returns an `InboxRow` if the event is
a recognized inbox event, otherwise `None`. The store inserts the row in
the same transaction as the event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from surogates.session.events import EventType


@dataclass(frozen=True, slots=True)
class InboxRow:
    kind: str
    title: str
    body: str | None
    payload: dict[str, Any]
    action_ref: dict[str, Any] | None


_TITLE_TRUNCATE = 120


def _truncate(value: str | None, limit: int = _TITLE_TRUNCATE) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"  # …


def build_inbox_row(
    *,
    event_type: EventType,
    event_data: dict[str, Any],
    session_id: str,
) -> InboxRow | None:
    if event_type == EventType.INBOX_INPUT_REQUIRED:
        return _input_required(event_data, session_id)
    if event_type == EventType.INBOX_TASK_COMPLETE:
        return _task_complete(event_data)
    if event_type == EventType.INBOX_GOVERNANCE_GATE:
        return _governance_gate(event_data)
    if event_type == EventType.INBOX_PROGRESS_CHECKIN:
        return _progress_checkin(event_data)
    return None


def _input_required(data: dict[str, Any], session_id: str) -> InboxRow:
    tool_call_id = data["tool_call_id"]
    questions = data.get("questions") or []
    first_prompt = questions[0]["prompt"] if questions else "Agent needs input"
    return InboxRow(
        kind="input_required",
        title=_truncate(first_prompt),
        body=data.get("context"),
        payload={
            "tool_call_id": tool_call_id,
            "questions": questions,
            "context": data.get("context", ""),
        },
        action_ref={
            "type": "clarify_response",
            "tool_call_id": tool_call_id,
            "endpoint": f"/v1/sessions/{session_id}/clarify/{tool_call_id}/respond",
        },
    )


def _task_complete(data: dict[str, Any]) -> InboxRow:
    title = data.get("session_title") or "Task complete"
    return InboxRow(
        kind="task_complete",
        title=_truncate(title),
        body=_truncate(data.get("summary") or "", limit=1000) or None,
        payload={
            "outcome": data.get("outcome", "success"),
            "summary": data.get("summary", ""),
            "duration_seconds": int(data.get("duration_seconds", 0)),
            "error": data.get("error"),
        },
        action_ref=None,
    )


def _governance_gate(data: dict[str, Any]) -> InboxRow:
    tool_name = data["tool_name"]
    return InboxRow(
        kind="governance_gate",
        title=_truncate(f"Approval needed: {tool_name}"),
        body=_truncate(
            f"{data.get('deny_reason', '')}\n\n{data.get('arguments_excerpt', '')}",
            limit=1000,
        )
        or None,
        payload={
            "tool_name": tool_name,
            "tool_call_id": data["tool_call_id"],
            "arguments_excerpt": data.get("arguments_excerpt", ""),
            "deny_reason": data.get("deny_reason", ""),
            "policy_id": data.get("policy_id"),
        },
        action_ref={
            "type": "governance_decision",
            "endpoint": "/v1/inbox/{item_id}/respond",
            "choices": ["approve", "reject"],
        },
    )


def _progress_checkin(data: dict[str, Any]) -> InboxRow:
    iterations = int(data.get("iterations", 0))
    elapsed = int(data.get("elapsed_seconds", 0))
    title = f"Progress: {iterations} iterations, {elapsed // 60} min elapsed"
    return InboxRow(
        kind="progress_checkin",
        title=_truncate(title),
        body=_truncate(data.get("progress_summary") or "", limit=1000) or None,
        payload={
            "progress_summary": data.get("progress_summary", ""),
            "iterations": iterations,
            "last_tool": data.get("last_tool", ""),
            "elapsed_seconds": elapsed,
        },
        action_ref=None,
    )
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `pytest tests/test_inbox_payload.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/inbox_payload.py tests/test_inbox_payload.py
git commit -m "feat(inbox): add inbox row payload builder"
```

---

### Task 4: Extend `SessionStore.emit_event` with the inbox write path

**Files:**
- Modify: `surogates/session/store.py`

This is the central change. Add `_INBOX_EVENTS`, write the `InboxItem` row in the same transaction as the event, and publish a post-commit Redis nudge.

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_emit.py`:

```python
"""SessionStore.emit_event writes to inbox_items for _INBOX_EVENTS and
skips for events outside the set or sessions without a user."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_inbox_row_written_for_inbox_event_type(
    session_store, sample_session_with_user
):
    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {
            "outcome": "success",
            "summary": "All done.",
            "duration_seconds": 7,
            "session_title": "Migrate users",
        },
    )
    async with session_store._sf() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()
    assert row.kind == "task_complete"
    assert row.user_id == sample_session_with_user.user_id
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_no_inbox_row_for_non_inbox_event(
    session_store, sample_session_with_user
):
    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.LLM_RESPONSE,
        {"text": "hi"},
    )
    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_anonymous_session_skips_inbox(
    session_store, sample_anonymous_session
):
    event_id = await session_store.emit_event(
        sample_anonymous_session.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).all()
    assert rows == []
```

If `session_store`, `sample_session_with_user`, or `sample_anonymous_session` fixtures don't exist, add them to `tests/conftest.py` (or wherever the existing session/store fixtures live). The user-bearing session needs both `user_id` and `org_id` set; the anonymous one has `user_id=None`.

- [ ] **Step 2: Run tests — verify they fail**

Run: `pytest tests/test_inbox_emit.py -v`
Expected: FAIL — `_INBOX_EVENTS` not defined and no inbox write happens.

- [ ] **Step 3: Add `_INBOX_EVENTS` and the in-transaction insert**

In `surogates/session/store.py`, near the top of the file alongside `_DELIVERABLE_EVENTS`, add:

```python
_INBOX_EVENTS = frozenset({
    EventType.INBOX_INPUT_REQUIRED,
    EventType.INBOX_TASK_COMPLETE,
    EventType.INBOX_GOVERNANCE_GATE,
    EventType.INBOX_PROGRESS_CHECKIN,
})
```

In `emit_event`, between the `db.add(row)` / `db.flush()` block and the existing `db.commit()`, add (inside the `async with self._sf() as db:` block):

```python
            # Inbox write path: mirror a curated subset of events into
            # inbox_items for the web inbox pane. See spec
            # docs/superpowers/specs/2026-05-11-agent-inbox-design.md.
            inbox_publish: tuple[int, str, uuid.UUID] | None = None
            if event_type in _INBOX_EVENTS:
                # Look up org/user for the session. The session row was
                # inserted at session-start time and is in the same DB.
                session_row = await db.get(SessionRow, session_id)
                if session_row is not None and session_row.user_id is not None:
                    from surogates.session.inbox_payload import build_inbox_row

                    inbox_row = build_inbox_row(
                        event_type=event_type,
                        event_data=redacted_data,
                        session_id=str(session_id),
                    )
                    if inbox_row is not None:
                        item = InboxItem(
                            org_id=session_row.org_id,
                            user_id=session_row.user_id,
                            session_id=session_id,
                            source_event_id=event_id,
                            kind=inbox_row.kind,
                            title=inbox_row.title,
                            body=inbox_row.body,
                            payload=inbox_row.payload,
                            action_ref=inbox_row.action_ref,
                        )
                        db.add(item)
                        await db.flush()
                        inbox_publish = (
                            item.id,
                            inbox_row.kind,
                            session_row.user_id,
                        )
```

Add the imports at the top of `store.py` if missing:

```python
from surogates.db.models import InboxItem  # add to the existing grouped import
```

After the existing `await self._redis.publish(f"surogates:session:{session_id}", ...)` block (post-commit), append:

```python
        # Inbox SSE nudge — post-commit, best-effort.
        if inbox_publish is not None and self._redis is not None:
            item_id, kind, user_id = inbox_publish
            try:
                await self._redis.publish(
                    f"surogates:inbox:{user_id}",
                    f"{item_id}:{kind}",
                )
            except Exception:
                pass
```

Make `inbox_publish` accessible outside the `async with` block by initializing it to `None` before the `async with`.

- [ ] **Step 4: Run tests — verify they pass**

Run: `pytest tests/test_inbox_emit.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Run the full store-related test suite**

Run: `pytest tests/test_inbox_emit.py tests/test_inbox_model.py tests/test_inbox_event_types.py tests/test_inbox_payload.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/session/store.py tests/test_inbox_emit.py
git commit -m "feat(inbox): write inbox_items in emit_event for INBOX_* events"
```

---

## Phase 2 — Hook points (emit the four events)

### Task 5: Emit `INBOX_INPUT_REQUIRED` from the clarify tool

**Files:**
- Modify: `surogates/tools/builtin/clarify.py`

The clarify handler already builds and normalizes the question list. Immediately after validation and before `_wait_for_response`, emit `INBOX_INPUT_REQUIRED`. The `tool_call_id` is the same one already in scope from the harness call.

- [ ] **Step 1: Read the existing handler**

Read: `surogates/tools/builtin/clarify.py`
Note: where the handler resolves `tool_call_id`, where it validates `questions`, and the call to `_wait_for_response` near line 337.

- [ ] **Step 2: Write the failing integration test**

Create `tests/test_inbox_clarify_hook.py`:

```python
"""When the clarify tool runs, it emits INBOX_INPUT_REQUIRED carrying the
tool_call_id, alongside the existing TOOL_CALL event."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_clarify_emits_inbox_input_required(
    session_store, sample_session_with_user, run_clarify_tool
):
    await run_clarify_tool(
        session_id=sample_session_with_user.id,
        tool_call_id="tc-clarify-1",
        questions=[{"prompt": "Which color?"}],
        context="brand kickoff",
    )
    async with session_store._sf() as db:
        row = (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.session_id == sample_session_with_user.id
                )
            )
        ).scalar_one()
    assert row.kind == "input_required"
    assert row.payload["tool_call_id"] == "tc-clarify-1"
    assert row.action_ref["tool_call_id"] == "tc-clarify-1"
```

`run_clarify_tool` is a helper fixture you add to `tests/conftest.py` that invokes the clarify handler synchronously without actually entering `_wait_for_response` (stub the wait to return immediately). If the existing clarify test (`tests/test_clarify.py` or similar) already has such a helper, reuse it.

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_inbox_clarify_hook.py -v`
Expected: FAIL — no inbox row written.

- [ ] **Step 4: Add the emit**

In `surogates/tools/builtin/clarify.py`, after question validation and before the call to `_wait_for_response`, add:

```python
    await session_store.emit_event(
        session_id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": str(tool_call_id),
            "questions": questions,
            "context": "",
        },
    )
```

`session_store`, `session_id`, `tool_call_id`, and `questions` are already local names in `_clarify_handler`. `EventType` is already imported in this file.

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_inbox_clarify_hook.py tests/test_inbox_emit.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/builtin/clarify.py tests/test_inbox_clarify_hook.py
git commit -m "feat(inbox): emit INBOX_INPUT_REQUIRED from clarify tool"
```

---

### Task 6: Emit `INBOX_TASK_COMPLETE` from `_complete_session`

**Files:**
- Modify: `surogates/harness/loop.py`

- [ ] **Step 1: Locate `_complete_session`**

Run: `grep -n '_complete_session' /work/surogates2/surogates/surogates/harness/loop.py`
Expect a hit around line 3047 per the spec.

- [ ] **Step 2: Write the failing test**

Create `tests/test_inbox_completion_hook.py`:

```python
"""When _complete_session runs, INBOX_TASK_COMPLETE is emitted carrying
the outcome and a short summary."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem


@pytest.mark.asyncio
async def test_complete_session_emits_inbox_task_complete(
    session_store, sample_session_with_user, sample_lease, harness_for_session_with_user
):
    await harness_for_session_with_user._complete_session(
        sample_session_with_user,
        [{"role": "assistant", "content": "All done."}],
        sample_lease,
        reason="done",
    )
    async with session_store._sf() as db:
        row = (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.session_id == sample_session_with_user.id
                )
            )
        ).scalar_one()
    assert row.kind == "task_complete"
    assert row.payload["outcome"] == "success"
```

`harness_for_session_with_user` is a fixture that builds an `AgentHarness`; `sample_lease` is a held `SessionLease` for `sample_session_with_user`. Mock or stub anything that touches external services (sandbox teardown, memory). Reuse fixtures from existing harness tests (`tests/test_harness_pending.py`, `tests/test_loop_*.py`).

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_inbox_completion_hook.py -v`
Expected: FAIL — no inbox row.

- [ ] **Step 4: Add the emit**

In `surogates/harness/loop.py:_complete_session`, immediately after the existing `await self._store.emit_event(session.id, EventType.SESSION_COMPLETE, complete_data)` call and before `update_session_status(...)`, add:

```python
        await self._store.emit_event(
            session.id,
            EventType.INBOX_TASK_COMPLETE,
            {
                "outcome": "success" if reason in {"stop", "done", "complete"} else reason,
                "summary": _last_assistant_message_excerpt(messages),
                "duration_seconds": int(
                    (datetime.now(timezone.utc) - session.created_at).total_seconds()
                ),
                "session_title": session.title or "Task complete",
                "error": None,
            },
        )
```

Add a module-level helper near the other small helpers in `loop.py`:

```python
def _last_assistant_message_excerpt(messages: list[dict], limit: int = 500) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            )
        text = str(content).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"
    return ""
```

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_inbox_completion_hook.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py tests/test_inbox_completion_hook.py
git commit -m "feat(inbox): emit INBOX_TASK_COMPLETE from _complete_session"
```

---

### Task 7: Add `overridable` flag to `PolicyDecision`

**Files:**
- Modify: `surogates/governance/policy.py`

- [ ] **Step 1: Locate `PolicyDecision`**

Run: `grep -n 'class PolicyDecision\|@dataclass' /work/surogates2/surogates/surogates/governance/policy.py | head -10`

- [ ] **Step 2: Write the failing test**

Create `tests/test_policy_overridable.py`:

```python
"""PolicyDecision gains an `overridable` field, default False."""

from surogates.governance.policy import PolicyDecision


def test_policy_decision_overridable_default_false():
    decision = PolicyDecision(
        allowed=False,
        reason="external",
        tool_name="send_email",
    )
    assert decision.overridable is False


def test_policy_decision_overridable_can_be_set_true():
    decision = PolicyDecision(
        allowed=False,
        reason="external",
        tool_name="send_email",
        overridable=True,
    )
    assert decision.overridable is True
```

If `PolicyDecision`'s constructor uses different keyword names, match them.

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_policy_overridable.py -v`
Expected: FAIL — unexpected keyword `overridable`.

- [ ] **Step 4: Add the field**

In the `PolicyDecision` dataclass (or pydantic model) in `surogates/governance/policy.py`, add:

```python
    overridable: bool = False
    """If True, an overridable user-resolvable denial: surfaced via the
    agent inbox as a governance_gate item. Default False — hard safety
    checks remain non-overridable. Set this to True only on policy rules
    that a human can legitimately approve."""
    policy_id: str | None = None
    """Optional identifier for the user-resolvable policy rule. Included
    in governance_gate inbox payloads for auditability."""
```

Audit all call sites that construct `PolicyDecision(...)` — they should continue to work since `overridable` defaults to `False`. Add `overridable=True` only where the spec calls for it (no existing policies need to change in this task; flagging happens in the policies that opt in).

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_policy_overridable.py tests/test_governance.py -v`
Expected: PASS (governance test must remain green).

- [ ] **Step 6: Commit**

```bash
git add surogates/governance/policy.py tests/test_policy_overridable.py
git commit -m "feat(governance): add overridable flag to PolicyDecision"
```

---

### Task 8: Emit `INBOX_GOVERNANCE_GATE` from `tool_exec.py`

**Files:**
- Modify: `surogates/harness/tool_exec.py`

- [ ] **Step 1: Locate the policy-denial branch**

Run: `grep -n 'POLICY_DENIED\|policy.*denied\|policy_decision\|allowed' /work/surogates2/surogates/surogates/harness/tool_exec.py | head -20`
Find the block that emits `POLICY_DENIED` and returns a tool-error result.

- [ ] **Step 2: Write the failing test**

Create `tests/test_inbox_governance_hook.py`:

```python
"""An overridable policy denial emits INBOX_GOVERNANCE_GATE carrying the
tool_name, tool_call_id, and policy_id."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.governance.policy import PolicyDecision


@pytest.mark.asyncio
async def test_overridable_denial_emits_inbox_governance_gate(
    session_store, sample_session_with_user, tool_exec_for, monkeypatch
):
    def fake_check(*args, **kwargs):
        return PolicyDecision(
            allowed=False,
            reason="External recipient requires explicit approval.",
            tool_name="read_file",
            overridable=True,
            policy_id="external-comms-v1",
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )

    await tool_exec_for(
        session_id=sample_session_with_user.id,
        tool_name="read_file",
        tool_call_id="tc-gov-1",
        arguments={"path": "/tmp/outside-workspace.txt"},
        session_config={"workspace_path": "/workspace"},
    )

    async with session_store._sf() as db:
        row = (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.session_id == sample_session_with_user.id,
                    InboxItem.kind == "governance_gate",
                )
            )
        ).scalar_one()

    assert row.payload["tool_name"] == "send_email"
    assert row.payload["tool_call_id"] == "tc-gov-1"
    assert row.payload["policy_id"] == "external-comms-v1"
    assert row.action_ref["choices"] == ["approve", "reject"]


@pytest.mark.asyncio
async def test_non_overridable_denial_does_not_emit_inbox(
    session_store, sample_session_with_user, tool_exec_for, monkeypatch
):
    def fake_check(*args, **kwargs):
        return PolicyDecision(
            allowed=False,
            reason="Hard safety.",
            tool_name="read_file",
            overridable=False,
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )
    await tool_exec_for(
        session_id=sample_session_with_user.id,
        tool_name="read_file",
        tool_call_id="tc-gov-2",
        arguments={"path": "/tmp/outside-workspace.txt"},
        session_config={"workspace_path": "/workspace"},
    )
    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.session_id == sample_session_with_user.id,
                )
            )
        ).all()
    assert rows == []
```

`tool_exec_for` is a helper fixture that calls the actual tool-exec entry point (not the LLM-driven loop) with a known tool name and arguments. Wire it up if not present.

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_inbox_governance_hook.py -v`
Expected: FAIL — no inbox row written on overridable denial.

- [ ] **Step 4: Add the emit**

In `surogates/harness/tool_exec.py`, in the branch that handles a denied `PolicyDecision`, add (just after the existing `POLICY_DENIED` emission). The currently active denial branch is the workspace-sandbox branch using a local `decision` variable:

```python
        if decision.overridable:
            await store.emit_event(
                session.id,
                EventType.INBOX_GOVERNANCE_GATE,
                {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "arguments_excerpt": _truncate_args(sanitized_args, limit=500),
                    "deny_reason": decision.reason or "",
                    "policy_id": getattr(decision, "policy_id", None),
                },
            )
```

`_truncate_args` is a small helper local to `tool_exec.py` (add it if absent):

```python
def _truncate_args(arguments: Any, limit: int = 500) -> str:
    import json
    raw = json.dumps(arguments, default=str)
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"
```

The tool result returned to the LLM remains the existing structured error. Per the spec, the harness does not auto-replay on approval — the LLM re-attempts based on event history.

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_inbox_governance_hook.py -v`
Expected: PASS (both cases).

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/tool_exec.py tests/test_inbox_governance_hook.py
git commit -m "feat(inbox): emit INBOX_GOVERNANCE_GATE on overridable policy denial"
```

---

### Task 9: Emit `INBOX_PROGRESS_CHECKIN` from the harness loop

**Files:**
- Modify: `surogates/harness/loop.py`

- [ ] **Step 1: Locate the iteration-checkpoint area of the loop**

Run: `grep -n 'iter\|iteration\|self._memory_nudge_interval\|self._skill_nudge_interval' /work/surogates2/surogates/surogates/harness/loop.py | head -20`
Find the place where memory/skill nudges fire each turn — the new check-in lives in the same neighborhood.

- [ ] **Step 2: Write the failing test**

Create `tests/test_inbox_progress_hook.py`:

```python
"""When inbox_checkin_interval_seconds is set and elapsed > interval,
the harness emits INBOX_PROGRESS_CHECKIN at iteration boundaries."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem


@pytest.mark.asyncio
async def test_progress_checkin_emitted_after_interval(
    session_store, harness_for_session_with_user, fast_clock
):
    h = harness_for_session_with_user
    h.session.config = {**(h.session.config or {}), "inbox_checkin_interval_seconds": 60}
    # Advance simulated clock past the interval.
    fast_clock.advance(seconds=120)
    await h._maybe_emit_progress_checkin(iteration_count=3, last_tool="shell_exec")
    async with session_store._sf() as db:
        row = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == h.session.id)
            )
        ).scalar_one()
    assert row.kind == "progress_checkin"
    assert row.payload["iterations"] == 3


@pytest.mark.asyncio
async def test_progress_checkin_skipped_when_disabled(
    session_store, harness_for_session_with_user, fast_clock
):
    h = harness_for_session_with_user
    h.session.config = {**(h.session.config or {}), "inbox_checkin_interval_seconds": None}
    fast_clock.advance(seconds=120)
    await h._maybe_emit_progress_checkin(iteration_count=3, last_tool="shell_exec")
    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == h.session.id)
            )
        ).all()
    assert rows == []
```

Add a `fast_clock` fixture if absent that monkeypatches `datetime.now` to advance under test control.

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_inbox_progress_hook.py -v`
Expected: FAIL — `_maybe_emit_progress_checkin` doesn't exist.

- [ ] **Step 4: Implement `_maybe_emit_progress_checkin`**

In `surogates/harness/loop.py`, add a method on `AgentHarness` near the other iteration-boundary helpers:

```python
    async def _maybe_emit_progress_checkin(
        self,
        session: Session,
        messages: list[dict],
        iteration_count: int,
        last_tool: str | None = None,
    ) -> None:
        """Emit INBOX_PROGRESS_CHECKIN if the configured interval has elapsed."""
        interval = (session.config or {}).get("inbox_checkin_interval_seconds")
        if not interval:
            return

        last = await self._store.last_event_at(
            session.id, EventType.INBOX_PROGRESS_CHECKIN
        )
        now = datetime.now(timezone.utc)
        reference = last or session.created_at
        if (now - reference).total_seconds() < interval:
            return

        elapsed = int((now - session.created_at).total_seconds())
        await self._store.emit_event(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
            {
                "progress_summary": _last_assistant_message_excerpt(messages),
                "iterations": iteration_count,
                "last_tool": last_tool or "",
                "elapsed_seconds": elapsed,
            },
        )
```

Add `last_event_at` to `SessionStore`:

```python
    async def last_event_at(
        self, session_id: uuid.UUID, event_type: EventType
    ) -> datetime | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(EventRow.created_at)
                    .where(
                        EventRow.session_id == session_id,
                        EventRow.type == event_type.value,
                    )
                    .order_by(EventRow.id.desc())
                    .limit(1)
                )
            ).first()
        return row[0] if row else None
```

In the main harness loop body, call `await self._maybe_emit_progress_checkin(session, messages, iteration_count=..., last_tool=last_tool_name)` at the natural iteration boundary (after each LLM turn completes, alongside the memory/skill nudge checks). Use the local iteration counter names already present in the loop.

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_inbox_progress_hook.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py surogates/session/store.py tests/test_inbox_progress_hook.py
git commit -m "feat(inbox): emit INBOX_PROGRESS_CHECKIN at harness iteration checkpoints"
```

---

## Phase 3 — API routes

### Task 10: Add `SessionStore` helpers for the API

**Files:**
- Modify: `surogates/session/store.py`

The API routes don't talk to SQLAlchemy directly — they go through helpers on `SessionStore`. Add `list_inbox`, `get_inbox_item`, `mark_inbox_read`, `set_inbox_status` here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_store_helpers.py`:

```python
"""SessionStore helpers for the inbox API."""

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_list_inbox_returns_items_for_user_only(
    session_store, sample_session_with_user, second_session_other_user
):
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    await session_store.emit_event(
        second_session_other_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    rows = await session_store.list_inbox(
        user_id=sample_session_with_user.user_id, limit=50
    )
    assert len(rows) == 1
    assert rows[0].user_id == sample_session_with_user.user_id


@pytest.mark.asyncio
async def test_mark_inbox_read_sets_read_at(
    session_store, sample_session_with_user
):
    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        item = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()

    updated = await session_store.mark_inbox_read(
        item_id=item.id, user_id=sample_session_with_user.user_id
    )
    assert updated.read_at is not None

    # Idempotent: calling again does not change read_at.
    first_read = updated.read_at
    again = await session_store.mark_inbox_read(
        item_id=item.id, user_id=sample_session_with_user.user_id
    )
    assert again.read_at == first_read


@pytest.mark.asyncio
async def test_set_inbox_status_rejects_invalid_transition(
    session_store, sample_session_with_user
):
    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        item = (
            await db.execute(
                select(InboxItem).where(InboxItem.source_event_id == event_id)
            )
        ).scalar_one()

    await session_store.set_inbox_status(
        item_id=item.id,
        user_id=sample_session_with_user.user_id,
        new_status="acknowledged",
    )
    with pytest.raises(ValueError):
        await session_store.set_inbox_status(
            item_id=item.id,
            user_id=sample_session_with_user.user_id,
            new_status="responded",  # already terminal
        )
```

- [ ] **Step 2: Run — verify they fail**

Run: `pytest tests/test_inbox_store_helpers.py -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Implement the helpers**

Append to `SessionStore` in `surogates/session/store.py`:

```python
    _INBOX_TERMINAL = frozenset({"acknowledged", "responded", "expired"})
    _INBOX_ALLOWED_TRANSITIONS = {
        "pending": frozenset({"acknowledged", "responded", "expired"}),
    }

    async def list_inbox(
        self,
        *,
        user_id: uuid.UUID,
        status: str | None = None,
        kind: str | None = None,
        session_id: uuid.UUID | None = None,
        cursor: tuple[datetime, int] | None = None,
        limit: int = 50,
    ) -> list[InboxItem]:
        stmt = select(InboxItem).where(InboxItem.user_id == user_id)
        if status:
            stmt = stmt.where(InboxItem.status == status)
        else:
            stmt = stmt.where(InboxItem.status != "expired")
        if kind:
            stmt = stmt.where(InboxItem.kind == kind)
        if session_id:
            stmt = stmt.where(InboxItem.session_id == session_id)
        if cursor:
            cursor_created_at, cursor_id = cursor
            stmt = stmt.where(
                tuple_(InboxItem.created_at, InboxItem.id)
                < tuple_(cursor_created_at, cursor_id)
            )
        stmt = stmt.order_by(InboxItem.created_at.desc(), InboxItem.id.desc()).limit(limit)
        async with self._sf() as db:
            return (await db.execute(stmt)).scalars().all()

    async def get_inbox_item(
        self, *, item_id: int, user_id: uuid.UUID
    ) -> InboxItem | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.id == item_id, InboxItem.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
        return row

    async def mark_inbox_read(
        self, *, item_id: int, user_id: uuid.UUID
    ) -> InboxItem:
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.id == item_id, InboxItem.user_id == user_id
                    )
                )
            ).scalar_one()
            if row.read_at is None:
                row.read_at = datetime.now(timezone.utc)
                await db.commit()
                await db.refresh(row)
            return row

    async def set_inbox_status(
        self, *, item_id: int, user_id: uuid.UUID, new_status: str
    ) -> InboxItem:
        if new_status not in {"acknowledged", "responded", "expired"}:
            raise ValueError(f"Invalid target status: {new_status}")
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.id == item_id, InboxItem.user_id == user_id
                    )
                )
            ).scalar_one()
            if row.status in self._INBOX_TERMINAL:
                raise ValueError(
                    f"Cannot transition from terminal status {row.status}"
                )
            allowed = self._INBOX_ALLOWED_TRANSITIONS.get(row.status, frozenset())
            if new_status not in allowed:
                raise ValueError(
                    f"Invalid transition {row.status} -> {new_status}"
                )
            row.status = new_status
            row.responded_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return row
```

Add `from sqlalchemy import tuple_` to the imports.

- [ ] **Step 4: Run — verify they pass**

Run: `pytest tests/test_inbox_store_helpers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/session/store.py tests/test_inbox_store_helpers.py
git commit -m "feat(inbox): add SessionStore helpers for inbox API"
```

---

### Task 11: Scaffold `surogates/api/routes/inbox.py` with `GET /v1/inbox`

**Files:**
- Create: `surogates/api/routes/inbox.py`
- Modify: `surogates/api/app.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_api.py`:

```python
"""API tests for /v1/inbox."""

import pytest


@pytest.mark.asyncio
async def test_list_inbox_returns_only_callers_items(
    api_client, sample_session_with_user, session_store
):
    from surogates.session.events import EventType

    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    resp = await api_client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == "task_complete"


@pytest.mark.asyncio
async def test_list_inbox_rejects_service_account(api_client, service_account_jwt):
    resp = await api_client.get(
        "/v1/inbox",
        headers={"Authorization": f"Bearer {service_account_jwt}"},
    )
    assert resp.status_code == 403
```

`api_client`, `sample_session_with_user.user_jwt`, and `service_account_jwt` are fixtures — mirror what other API tests use (`tests/test_channels.py`, `tests/test_browser_route_ws.py` etc.).

- [ ] **Step 2: Run — verify it fails**

Run: `pytest tests/test_inbox_api.py::test_list_inbox_returns_only_callers_items -v`
Expected: FAIL — 404, route not mounted.

- [ ] **Step 3: Create the router**

Create `surogates/api/routes/inbox.py`:

```python
"""HTTP routes for the agent inbox.

Spec: docs/superpowers/specs/2026-05-11-agent-inbox-design.md

All routes require an authenticated user (not a service account). Items
are scoped strictly by `tenant.user_id`; cross-user access returns 404.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter(prefix="/inbox")


def _require_user_tenant(tenant: TenantContext) -> TenantContext:
    if tenant.user_id is None:
        raise HTTPException(status_code=403, detail="Inbox requires a user account.")
    return tenant


def _encode_cursor(created_at: datetime, item_id: int) -> str:
    raw = json.dumps([created_at.isoformat(), item_id])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        iso, item_id = json.loads(raw)
        return datetime.fromisoformat(iso), int(item_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from exc


def _serialize_item(item) -> dict:
    return {
        "id": item.id,
        "org_id": str(item.org_id),
        "user_id": str(item.user_id),
        "session_id": str(item.session_id),
        "source_event_id": item.source_event_id,
        "kind": item.kind,
        "status": item.status,
        "title": item.title,
        "body": item.body,
        "payload": item.payload,
        "action_ref": item.action_ref,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "responded_at": item.responded_at.isoformat() if item.responded_at else None,
    }


@router.get("")
async def list_inbox(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    items = await store.list_inbox(
        user_id=tenant.user_id,
        status=status,
        kind=kind,
        session_id=UUID(session_id) if session_id else None,
        cursor=_decode_cursor(cursor),
        limit=limit,
    )
    next_cursor = (
        _encode_cursor(items[-1].created_at, items[-1].id) if len(items) == limit else None
    )
    return {
        "items": [_serialize_item(i) for i in items],
        "next_cursor": next_cursor,
    }
```

In `surogates/api/app.py`, alongside the other `app.include_router(...)` calls, add:

```python
from surogates.api.routes import inbox
app.include_router(inbox.router, prefix="/v1", tags=["inbox"])
```

- [ ] **Step 4: Run — verify both list tests pass**

Run: `pytest tests/test_inbox_api.py -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/inbox.py surogates/api/app.py tests/test_inbox_api.py
git commit -m "feat(inbox): add GET /v1/inbox route"
```

---

### Task 12: `GET /v1/inbox/{id}`, `POST /read`, `POST /ack`

**Files:**
- Modify: `surogates/api/routes/inbox.py`
- Modify: `tests/test_inbox_api.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_inbox_api.py`:

```python
@pytest.mark.asyncio
async def test_get_inbox_item(api_client, session_store, sample_session_with_user):
    from surogates.session.events import EventType

    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.source_event_id == event_id)
        )).scalar_one()

    resp = await api_client.get(
        f"/v1/inbox/{item.id}",
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == item.id


@pytest.mark.asyncio
async def test_get_other_users_item_returns_404(
    api_client, session_store, sample_session_with_user, other_user_jwt
):
    from surogates.session.events import EventType

    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.source_event_id == event_id)
        )).scalar_one()

    resp = await api_client.get(
        f"/v1/inbox/{item.id}",
        headers={"Authorization": f"Bearer {other_user_jwt}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mark_read_is_idempotent(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType

    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.source_event_id == event_id)
        )).scalar_one()

    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}
    r1 = await api_client.post(f"/v1/inbox/{item.id}/read", headers=headers)
    r2 = await api_client.post(f"/v1/inbox/{item.id}/read", headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["read_at"] == r2.json()["read_at"]


@pytest.mark.asyncio
async def test_ack_flips_status_to_acknowledged(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType

    event_id = await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.source_event_id == event_id)
        )).scalar_one()

    resp = await api_client.post(
        f"/v1/inbox/{item.id}/ack",
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_ack_rejects_non_ackable_kind(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType

    # Emit an INBOX_INPUT_REQUIRED item (not ackable).
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-x",
            "questions": [{"prompt": "?"}],
            "context": "",
        },
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()

    resp = await api_client.post(
        f"/v1/inbox/{item.id}/ack",
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run — verify they fail**

Run: `pytest tests/test_inbox_api.py -v`
Expected: 4 new failures.

- [ ] **Step 3: Add the routes**

Append to `surogates/api/routes/inbox.py`:

```python
_ACKABLE_KINDS = frozenset({"task_complete", "progress_checkin"})


@router.get("/{item_id}")
async def get_inbox_item(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    tenant = _require_user_tenant(tenant)
    item = await request.app.state.session_store.get_inbox_item(
        item_id=item_id, user_id=tenant.user_id
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Inbox item not found.")
    return _serialize_item(item)


@router.post("/{item_id}/read")
async def mark_read(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(item_id=item_id, user_id=tenant.user_id)
    if item is None:
        raise HTTPException(status_code=404)
    updated = await store.mark_inbox_read(item_id=item_id, user_id=tenant.user_id)
    return _serialize_item(updated)


@router.post("/{item_id}/ack")
async def ack_inbox_item(
    item_id: int,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(item_id=item_id, user_id=tenant.user_id)
    if item is None:
        raise HTTPException(status_code=404)
    if item.kind not in _ACKABLE_KINDS:
        raise HTTPException(
            status_code=409,
            detail=f"Items of kind '{item.kind}' are not ack-able.",
        )
    try:
        updated = await store.set_inbox_status(
            item_id=item_id, user_id=tenant.user_id, new_status="acknowledged"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_item(updated)
```

- [ ] **Step 4: Run — verify they pass**

Run: `pytest tests/test_inbox_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/inbox.py tests/test_inbox_api.py
git commit -m "feat(inbox): add GET /{id}, POST /read, POST /ack routes"
```

---

### Task 13: `POST /v1/inbox/{id}/respond` (governance decision)

**Files:**
- Modify: `surogates/api/routes/inbox.py`
- Modify: `surogates/orchestrator/...` — find the wake-session call used by the existing clarify endpoint and reuse

- [ ] **Step 1: Append the failing test**

Append to `tests/test_inbox_api.py`:

```python
@pytest.mark.asyncio
async def test_respond_governance_records_decision_and_wakes_session(
    api_client, session_store, sample_session_with_user, monkeypatch
):
    from surogates.session.events import EventType

    # Create a governance_gate item.
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_GOVERNANCE_GATE,
        {
            "tool_name": "send_email",
            "tool_call_id": "tc-gov-3",
            "arguments_excerpt": "to=ceo@…",
            "deny_reason": "External recipient",
            "policy_id": "external-comms-v1",
        },
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()

    woken = []

    async def fake_wake(request, session_id):
        woken.append(session_id)

    monkeypatch.setattr(
        "surogates.api.routes.inbox._wake_session_from_request", fake_wake, raising=False
    )

    resp = await api_client.post(
        f"/v1/inbox/{item.id}/respond",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "responded"
    assert sample_session_with_user.id in woken

    # And a USER_MESSAGE event was emitted summarizing the decision.
    from surogates.db.models import Event
    async with session_store._sf() as db:
        rows = (
            await db.execute(
                select(Event)
                .where(
                    Event.session_id == sample_session_with_user.id,
                    Event.type == "user.message",
                )
            )
        ).all()
    assert rows, "expected a user.message event recording the user decision"


@pytest.mark.asyncio
async def test_respond_rejects_non_governance_kind(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_TASK_COMPLETE,
        {"outcome": "success", "duration_seconds": 1, "summary": "."},
    )
    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()
    resp = await api_client.post(
        f"/v1/inbox/{item.id}/respond",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {sample_session_with_user.user_jwt}"},
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run — verify they fail**

Run: `pytest tests/test_inbox_api.py -v -k respond`
Expected: failures.

- [ ] **Step 3: Add the route + helper**

Append to `surogates/api/routes/inbox.py`:

```python
from pydantic import BaseModel, Field

from surogates.config import enqueue_session
from surogates.session.events import EventType


class GovernanceDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


async def _wake_session_from_request(request: Request, session_id) -> None:
    session = await request.app.state.session_store.get_session(session_id)
    await enqueue_session(request.app.state.redis, session.agent_id, session_id)


@router.post("/{item_id}/respond")
async def respond(
    item_id: int,
    payload: GovernanceDecision,
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    tenant = _require_user_tenant(tenant)
    store = request.app.state.session_store
    item = await store.get_inbox_item(item_id=item_id, user_id=tenant.user_id)
    if item is None:
        raise HTTPException(status_code=404)
    if item.kind != "governance_gate":
        raise HTTPException(
            status_code=409,
            detail=f"Items of kind '{item.kind}' are not respond-able here.",
        )

    decision = payload.decision
    tool_name = item.payload.get("tool_name", "unknown")
    tool_call_id = item.payload.get("tool_call_id", "")
    user_message = (
        f"[governance decision] {decision.upper()} for {tool_name}"
        f" (call {tool_call_id})."
    )
    await store.emit_event(
        item.session_id,
        EventType.USER_MESSAGE,
        {"content": user_message, "source": "inbox_governance_decision"},
    )
    try:
        updated = await store.set_inbox_status(
            item_id=item_id, user_id=tenant.user_id, new_status="responded"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _wake_session_from_request(request, item.session_id)
    return _serialize_item(updated)
```

- [ ] **Step 4: Run — verify they pass**

Run: `pytest tests/test_inbox_api.py -v -k respond`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/inbox.py tests/test_inbox_api.py
git commit -m "feat(inbox): add POST /respond for governance decisions"
```

---

### Task 14: `GET /v1/inbox/stream` (SSE)

**Files:**
- Modify: `surogates/api/routes/inbox.py`

- [ ] **Step 1: Append the failing test**

Append to `tests/test_inbox_api.py`:

```python
@pytest.mark.asyncio
async def test_sse_stream_emits_nudge_for_new_item(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType
    import asyncio

    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}

    async with api_client.stream("GET", "/v1/inbox/stream", headers=headers) as resp:
        assert resp.status_code == 200

        async def emit():
            await asyncio.sleep(0.1)
            await session_store.emit_event(
                sample_session_with_user.id,
                EventType.INBOX_TASK_COMPLETE,
                {"outcome": "success", "duration_seconds": 1, "summary": "."},
            )

        emitter = asyncio.create_task(emit())
        saw_nudge = False
        async for chunk in resp.aiter_text():
            if "task_complete" in chunk:
                saw_nudge = True
                break
        await emitter
        assert saw_nudge
```

This test runs against the real Redis connection used by `session_store`; the test fixture must wire `app.state.redis` to the same Redis as the store.

- [ ] **Step 2: Run — verify it fails**

Run: `pytest tests/test_inbox_api.py -v -k sse_stream`
Expected: FAIL — route not present.

- [ ] **Step 3: Add the SSE route**

Add this route in `surogates/api/routes/inbox.py` **before** `@router.get("/{item_id}")`; otherwise the dynamic `{item_id}` route can capture `/stream`.

```python
import asyncio

from sse_starlette.sse import EventSourceResponse


@router.get("/stream")
async def stream(
    request: Request,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    tenant = _require_user_tenant(tenant)
    redis = request.app.state.redis
    channel = f"surogates:inbox:{tenant.user_id}"

    async def event_gen():
        # Initial snapshot of unread, non-expired item ids.
        store = request.app.state.session_store
        snapshot = await store.list_inbox(user_id=tenant.user_id, limit=200)
        unread_ids = [i.id for i in snapshot if i.read_at is None and i.status != "expired"]
        yield {"event": "snapshot", "data": json.dumps({"unread_ids": unread_ids})}

        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode()
                if not raw:
                    continue
                item_id, kind = raw.split(":", 1)
                yield {
                    "event": "item",
                    "data": json.dumps({"item_id": int(item_id), "kind": kind}),
                }
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_gen())
```

- [ ] **Step 4: Run — verify it passes**

Run: `pytest tests/test_inbox_api.py -v -k sse_stream`
Expected: PASS.

- [ ] **Step 5: Run the full inbox suite**

Run: `pytest tests/ -v -k inbox`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/api/routes/inbox.py tests/test_inbox_api.py
git commit -m "feat(inbox): add GET /stream SSE endpoint"
```

---

### Task 15: Clarify response endpoint flips inbox status to `responded`

**Files:**
- Modify: `surogates/api/routes/clarify.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_api.py` (or create `tests/test_inbox_clarify_response.py`):

```python
@pytest.mark.asyncio
async def test_clarify_response_flips_inbox_to_responded(
    api_client, session_store, sample_session_with_user
):
    from surogates.session.events import EventType
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "tc-clr-1",
            "questions": [{"prompt": "Which color?"}],
            "context": "",
        },
    )
    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}
    resp = await api_client.post(
        f"/v1/sessions/{sample_session_with_user.id}/clarify/tc-clr-1/respond",
        json={"responses": [{"question": "Which color?", "answer": "blue"}]},
        headers=headers,
    )
    assert resp.status_code == 201

    async with session_store._sf() as db:
        from sqlalchemy import select
        from surogates.db.models import InboxItem
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()
    assert item.status == "responded"
    assert item.responded_at is not None
```

- [ ] **Step 2: Run — verify it fails**

Run: `pytest tests/test_inbox_api.py -v -k clarify_response`
Expected: FAIL — status remains `pending`.

- [ ] **Step 3: Add the post-hook in the clarify endpoint**

In `surogates/api/routes/clarify.py`, after the existing emit of `CLARIFY_RESPONSE`, add:

```python
    # Flip the matching inbox item (if any) to responded. Best-effort —
    # the clarify response is the source of truth in the event log; the
    # inbox update is a UI convenience.
    try:
        store = request.app.state.session_store
        async with store._sf() as db:
            from sqlalchemy import func, update
            from surogates.db.models import InboxItem
            stmt = (
                update(InboxItem)
                .where(
                    InboxItem.session_id == session_id,
                    InboxItem.kind == "input_required",
                    InboxItem.action_ref["tool_call_id"].as_string() == tool_call_id,
                    InboxItem.status == "pending",
                )
                .values(status="responded", responded_at=func.now())
            )
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.exception("Failed to flip inbox item to responded; not fatal.")
```

Add necessary imports (`from sqlalchemy import func` etc).

- [ ] **Step 4: Run — verify it passes**

Run: `pytest tests/test_inbox_api.py -v -k clarify_response`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/clarify.py tests/test_inbox_api.py
git commit -m "feat(inbox): flip inbox status to responded on clarify response"
```

---

## Phase 4 — Sweeper

### Task 16: `jobs/inbox_expire.py`

**Files:**
- Create: `surogates/jobs/inbox_expire.py`
- Modify: wherever cron-style jobs are registered (per `surogates/jobs/reset_idle_sessions.py` pattern)

- [ ] **Step 1: Locate the existing job registration pattern**

Run: `grep -rn 'reset_idle_sessions\|cleanup_sessions\|jobs\.' /work/surogates2/surogates/surogates/ | head -10`
Identify how existing jobs are wired and scheduled.

- [ ] **Step 2: Write the failing test**

Create `tests/test_inbox_expire.py`:

```python
"""The sweeper flips pending inbox items to expired when their session is
terminal."""

import pytest
from sqlalchemy import select, update

from surogates.db.models import InboxItem, Session
from surogates.jobs.inbox_expire import expire_inbox_items
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_sweeper_expires_pending_items_for_terminal_sessions(
    session_store, sample_session_with_user
):
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_INPUT_REQUIRED,
        {"tool_call_id": "tc-z", "questions": [], "context": ""},
    )
    # Mark the session as completed.
    async with session_store._sf() as db:
        await db.execute(
            update(Session)
            .where(Session.id == sample_session_with_user.id)
            .values(status="completed")
        )
        await db.commit()

    expired_count = await expire_inbox_items(session_store)
    assert expired_count >= 1

    async with session_store._sf() as db:
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()
    assert item.status == "expired"


@pytest.mark.asyncio
async def test_sweeper_does_not_touch_active_sessions(
    session_store, sample_session_with_user
):
    await session_store.emit_event(
        sample_session_with_user.id,
        EventType.INBOX_INPUT_REQUIRED,
        {"tool_call_id": "tc-z", "questions": [], "context": ""},
    )
    await expire_inbox_items(session_store)
    async with session_store._sf() as db:
        item = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == sample_session_with_user.id)
        )).scalar_one()
    assert item.status == "pending"
```

- [ ] **Step 3: Run — verify it fails**

Run: `pytest tests/test_inbox_expire.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement the sweeper**

Create `surogates/jobs/inbox_expire.py`:

```python
"""Background job: expire pending inbox items for terminal sessions.

Runs on a cron tick (every 5 minutes by default). Sets status='expired'
for `inbox_items` rows whose status is 'pending' and whose session is in
a terminal state (completed, failed, archived). Does not delete rows.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, update

from surogates.db.models import InboxItem, Session

logger = logging.getLogger(__name__)

_TERMINAL_SESSION_STATUSES = frozenset({"completed", "failed", "archived"})


async def expire_inbox_items(session_store) -> int:
    """Mark inbox items expired for terminal sessions. Returns the row count."""
    async with session_store._sf() as db:
        stmt = (
            update(InboxItem)
            .where(
                InboxItem.status == "pending",
                InboxItem.session_id.in_(
                    Session.__table__.select()
                    .with_only_columns(Session.id)
                    .where(Session.status.in_(_TERMINAL_SESSION_STATUSES))
                ),
            )
            .values(status="expired", updated_at=func.now())
            .returning(InboxItem.id)
        )
        result = await db.execute(stmt)
        ids = result.scalars().all()
        await db.commit()
    if ids:
        logger.info("inbox_expire: marked %d items expired", len(ids))
    return len(ids)
```

- [ ] **Step 5: Run — verify it passes**

Run: `pytest tests/test_inbox_expire.py -v`
Expected: PASS (both cases).

- [ ] **Step 6: Wire into worker startup**

Match the existing long-running background-task pattern in `surogates/orchestrator/worker.py` (see `scheduled_runner.run_forever()`). Add a small loop that calls `expire_inbox_items(session_store)` every 300 seconds while the worker is running, cancels it during shutdown, and treats errors as logged/non-fatal.

If the existing pattern is hard-coded job classes, follow the same form (look at `reset_idle_sessions.py`'s registration).

- [ ] **Step 7: Commit**

```bash
git add surogates/jobs/inbox_expire.py tests/test_inbox_expire.py
git commit -m "feat(inbox): sweeper expires pending items for terminal sessions"
```

---

## Phase 5 — Web frontend (web/src/)

The surogates web SPA uses TypeScript, React, TanStack Router, and shadcn UI primitives. The chat feature is the closest reference — read `web/src/features/chat/chat-page.tsx` before starting.

### Task 17: Inbox API client (TypeScript)

**Files:**
- Create: `web/src/api/inbox.ts`
- Create: `web/src/api/inbox.test.ts` (if the SPA has a Vitest setup; check `web/package.json`)

- [ ] **Step 1: Read existing API client patterns**

Read: `web/src/api/` — note conventions (axios? fetch wrapper? auth header injection?).

- [ ] **Step 2: Create the API client**

Create `web/src/api/inbox.ts`:

```typescript
import { authFetch } from "./auth";
import { parseError } from "./_errors";
import { getAuthToken } from "@/features/auth";

export type InboxKind =
  | "input_required"
  | "task_complete"
  | "governance_gate"
  | "progress_checkin";

export type InboxStatus =
  | "pending"
  | "acknowledged"
  | "responded"
  | "expired";

export interface InboxItem {
  id: number;
  org_id: string;
  user_id: string;
  session_id: string;
  source_event_id: number;
  kind: InboxKind;
  status: InboxStatus;
  title: string;
  body: string | null;
  payload: Record<string, unknown>;
  action_ref: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  read_at: string | null;
  responded_at: string | null;
}

export interface InboxListResponse {
  items: InboxItem[];
  next_cursor: string | null;
}

export interface ListOpts {
  status?: InboxStatus;
  kind?: InboxKind;
  sessionId?: string;
  cursor?: string;
  limit?: number;
}

export async function listInbox(opts: ListOpts = {}): Promise<InboxListResponse> {
  const params = new URLSearchParams();
  if (opts.status) params.set("status", opts.status);
  if (opts.kind) params.set("kind", opts.kind);
  if (opts.sessionId) params.set("session_id", opts.sessionId);
  if (opts.cursor) params.set("cursor", opts.cursor);
  if (opts.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const response = await authFetch(`/api/v1/inbox${qs ? `?${qs}` : ""}`);
  if (!response.ok) return parseError(response, "Failed to fetch inbox");
  return (await response.json()) as InboxListResponse;
}

export async function getInboxItem(id: number): Promise<InboxItem> {
  const response = await authFetch(`/api/v1/inbox/${id}`);
  if (!response.ok) return parseError(response, "Failed to fetch inbox item");
  return (await response.json()) as InboxItem;
}

export async function markRead(id: number): Promise<InboxItem> {
  const response = await authFetch(`/api/v1/inbox/${id}/read`, { method: "POST" });
  if (!response.ok) return parseError(response, "Failed to mark inbox item read");
  return (await response.json()) as InboxItem;
}

export async function ackItem(id: number): Promise<InboxItem> {
  const response = await authFetch(`/api/v1/inbox/${id}/ack`, { method: "POST" });
  if (!response.ok) return parseError(response, "Failed to acknowledge inbox item");
  return (await response.json()) as InboxItem;
}

export async function respondGovernance(
  id: number,
  decision: "approve" | "reject",
): Promise<InboxItem> {
  const response = await authFetch(`/api/v1/inbox/${id}/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  if (!response.ok) return parseError(response, "Failed to respond to inbox item");
  return (await response.json()) as InboxItem;
}

export function openInboxStream(
  onItem: (msg: { item_id: number; kind: InboxKind }) => void,
  onSnapshot: (msg: { unread_ids: number[] }) => void,
): EventSource {
  const url = new URL("/api/v1/inbox/stream", window.location.origin);
  const token = getAuthToken();
  if (token) url.searchParams.set("token", token);
  const es = new EventSource(url.toString());
  es.addEventListener("item", (e: MessageEvent) => {
    onItem(JSON.parse(e.data));
  });
  es.addEventListener("snapshot", (e: MessageEvent) => {
    onSnapshot(JSON.parse(e.data));
  });
  return es;
}
```

This follows the existing `web/src/api/sessions.ts` and `web/src/api/clarify.ts` style: `authFetch`, `/api/v1/...` paths, and `parseError`.

- [ ] **Step 3: Commit**

```bash
git add web/src/api/inbox.ts
git commit -m "feat(web): add inbox API client and SSE helper"
```

---

### Task 18: SSE hook + global unread count store

**Files:**
- Create: `web/src/features/inbox/use-inbox-stream.ts`
- Modify: `web/src/stores/` (or wherever shared client state lives) — add an `inboxUnreadCount` store

- [ ] **Step 1: Read existing store patterns**

Read: `web/src/stores/` — confirm whether the project uses Zustand, Jotai, React Context, etc.

- [ ] **Step 2: Add the unread-count store**

Create `web/src/stores/inbox-store.ts` (Zustand example — adapt to actual stack):

```typescript
import { create } from "zustand";

interface InboxState {
  unreadCount: number;
  setUnreadCount: (n: number) => void;
  increment: () => void;
  decrement: () => void;
}

export const useInboxStore = create<InboxState>((set) => ({
  unreadCount: 0,
  setUnreadCount: (n) => set({ unreadCount: n }),
  increment: () => set((s) => ({ unreadCount: s.unreadCount + 1 })),
  decrement: () => set((s) => ({ unreadCount: Math.max(0, s.unreadCount - 1) })),
}));
```

- [ ] **Step 3: Create the stream hook**

Create `web/src/features/inbox/use-inbox-stream.ts`:

```typescript
import { useEffect } from "react";

import { openInboxStream, listInbox } from "@/api/inbox";
import { useInboxStore } from "@/stores/inbox-store";

export function useInboxStream(onNewItem?: (itemId: number) => void) {
  const setUnreadCount = useInboxStore((s) => s.setUnreadCount);
  const increment = useInboxStore((s) => s.increment);

  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;

    // Initial unread count via list query (fallback for snapshot timing).
    listInbox({ status: "pending", limit: 200 }).then((resp) => {
      if (cancelled) return;
      const unread = resp.items.filter((i) => !i.read_at).length;
      setUnreadCount(unread);
    });

    es = openInboxStream(
      (msg) => {
        increment();
        onNewItem?.(msg.item_id);
      },
      (snap) => {
        setUnreadCount(snap.unread_ids.length);
      },
    );

    return () => {
      cancelled = true;
      es?.close();
    };
  }, [setUnreadCount, increment, onNewItem]);
}
```

- [ ] **Step 4: Commit**

```bash
git add web/src/stores/inbox-store.ts web/src/features/inbox/use-inbox-stream.ts
git commit -m "feat(web): add inbox unread store and SSE stream hook"
```

---

### Task 19: Inbox list view

**Files:**
- Create: `web/src/features/inbox/inbox-list.tsx`

- [ ] **Step 1: Implement the list**

Create `web/src/features/inbox/inbox-list.tsx`:

```typescript
import { useEffect, useState } from "react";
import { formatDistanceToNow } from "date-fns";

import { listInbox, InboxItem } from "@/api/inbox";

interface Props {
  selectedId?: number;
  onSelect: (id: number) => void;
}

const KIND_LABEL: Record<InboxItem["kind"], string> = {
  input_required: "Input needed",
  task_complete: "Task complete",
  governance_gate: "Approval",
  progress_checkin: "Progress",
};

export function InboxList({ selectedId, onSelect }: Props) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    listInbox({ limit: 50 }).then((resp) => {
      setItems(resp.items);
      setCursor(resp.next_cursor);
      setLoading(false);
    });
  }, []);

  async function loadMore() {
    if (!cursor) return;
    setLoading(true);
    const resp = await listInbox({ cursor, limit: 50 });
    setItems((prev) => [...prev, ...resp.items]);
    setCursor(resp.next_cursor);
    setLoading(false);
  }

  return (
    <div className="flex flex-col divide-y">
      {items.map((item) => (
        <button
          key={item.id}
          className={`flex flex-col items-start p-3 text-left hover:bg-accent ${
            item.id === selectedId ? "bg-accent" : ""
          } ${!item.read_at ? "font-medium" : "font-normal"}`}
          onClick={() => onSelect(item.id)}
        >
          <div className="flex w-full items-center justify-between">
            <span className="text-xs uppercase text-muted-foreground">
              {KIND_LABEL[item.kind]}
            </span>
            <span className="text-xs text-muted-foreground">
              {formatDistanceToNow(new Date(item.created_at))}
            </span>
          </div>
          <div className="mt-1 text-sm">{item.title}</div>
          <div className="mt-1 text-xs text-muted-foreground capitalize">
            {item.status}
          </div>
        </button>
      ))}
      {cursor && (
        <button
          className="p-3 text-sm text-muted-foreground hover:bg-accent"
          onClick={loadMore}
          disabled={loading}
        >
          {loading ? "Loading..." : "Load more"}
        </button>
      )}
    </div>
  );
}
```

If `formatDistanceToNow` doesn't exist, use `date-fns`'s function directly (it's a common SPA dep) or write a 5-line helper.

- [ ] **Step 2: Commit**

```bash
git add web/src/features/inbox/inbox-list.tsx
git commit -m "feat(web): add inbox list view"
```

---

### Task 20: Per-kind detail views

**Files:**
- Create: `web/src/features/inbox/detail/input-required.tsx`
- Create: `web/src/features/inbox/detail/task-complete.tsx`
- Create: `web/src/features/inbox/detail/governance-gate.tsx`
- Create: `web/src/features/inbox/detail/progress-checkin.tsx`
- Create: `web/src/features/inbox/inbox-detail.tsx` (dispatch by kind)

- [ ] **Step 1: Create the input-required detail**

Create `web/src/features/inbox/detail/input-required.tsx`:

```typescript
import { useState } from "react";

import { InboxItem } from "@/api/inbox";

interface Question {
  prompt: string;
  allow_other?: boolean;
  choices?: { label: string; description?: string }[];
}

interface Props {
  item: InboxItem;
  onSubmit: (answers: Record<string, string>) => Promise<void>;
}

export function InputRequiredDetail({ item, onSubmit }: Props) {
  const questions = (item.payload.questions as Question[]) ?? [];
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    setSubmitting(true);
    try {
      await onSubmit(answers);
    } finally {
      setSubmitting(false);
    }
  }

  const disabled = item.status !== "pending" || submitting;

  return (
    <div className="flex flex-col gap-4 p-6">
      {item.body && (
        <p className="text-sm text-muted-foreground whitespace-pre-wrap">
          {item.body}
        </p>
      )}
      {questions.map((q) => (
        <div key={q.prompt} className="flex flex-col gap-1">
          <label className="text-sm font-medium">{q.prompt}</label>
          {q.choices ? (
            <select
              className="rounded border p-2"
              value={answers[q.prompt] ?? ""}
              onChange={(e) =>
                setAnswers((prev) => ({ ...prev, [q.prompt]: e.target.value }))
              }
              disabled={disabled}
            >
              <option value="">Select…</option>
              {q.choices.map((c) => (
                <option key={c.label} value={c.label}>{c.label}</option>
              ))}
            </select>
          ) : (
            <input
              className="rounded border p-2"
              type="text"
              value={answers[q.prompt] ?? ""}
              onChange={(e) =>
                setAnswers((prev) => ({ ...prev, [q.prompt]: e.target.value }))
              }
              disabled={disabled}
            />
          )}
        </div>
      ))}
      <button
        className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
        onClick={handleSubmit}
        disabled={disabled}
      >
        {submitting ? "Submitting..." : "Submit"}
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Create the task-complete detail**

Create `web/src/features/inbox/detail/task-complete.tsx`:

```typescript
import { InboxItem } from "@/api/inbox";

interface Props {
  item: InboxItem;
  onAck: () => Promise<void>;
}

export function TaskCompleteDetail({ item, onAck }: Props) {
  const outcome = (item.payload.outcome as string) ?? "success";
  const error = item.payload.error as string | null;
  const duration = (item.payload.duration_seconds as number) ?? 0;

  return (
    <div className="flex flex-col gap-3 p-6">
      <div className={`inline-block w-fit rounded-full px-2 py-1 text-xs uppercase ${
        outcome === "success"
          ? "bg-green-100 text-green-800"
          : outcome === "failure"
            ? "bg-red-100 text-red-800"
            : "bg-yellow-100 text-yellow-800"
      }`}>
        {outcome}
      </div>
      {item.body && (
        <p className="text-sm whitespace-pre-wrap">{item.body}</p>
      )}
      {error && (
        <pre className="rounded bg-red-50 p-3 text-xs text-red-900">{error}</pre>
      )}
      <p className="text-xs text-muted-foreground">
        Duration: {Math.round(duration / 60)} min ({duration} s)
      </p>
      {item.status === "pending" && (
        <button
          className="self-start rounded bg-primary px-4 py-2 text-primary-foreground"
          onClick={onAck}
        >
          Acknowledge
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create the governance-gate detail**

Create `web/src/features/inbox/detail/governance-gate.tsx`:

```typescript
import { InboxItem } from "@/api/inbox";

interface Props {
  item: InboxItem;
  onDecision: (decision: "approve" | "reject") => Promise<void>;
}

export function GovernanceGateDetail({ item, onDecision }: Props) {
  const toolName = (item.payload.tool_name as string) ?? "tool";
  const args = (item.payload.arguments_excerpt as string) ?? "";
  const reason = (item.payload.deny_reason as string) ?? "";
  const disabled = item.status !== "pending";

  return (
    <div className="flex flex-col gap-4 p-6">
      <h3 className="text-lg font-semibold">Approve {toolName}?</h3>
      <div className="rounded border p-3">
        <p className="text-sm text-muted-foreground">{reason}</p>
        <pre className="mt-2 overflow-x-auto rounded bg-muted p-2 text-xs">
          {args}
        </pre>
      </div>
      <div className="flex gap-3">
        <button
          className="rounded bg-green-600 px-4 py-2 text-white disabled:opacity-50"
          onClick={() => onDecision("approve")}
          disabled={disabled}
        >
          Approve
        </button>
        <button
          className="rounded bg-red-600 px-4 py-2 text-white disabled:opacity-50"
          onClick={() => onDecision("reject")}
          disabled={disabled}
        >
          Reject
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Create the progress-checkin detail**

Create `web/src/features/inbox/detail/progress-checkin.tsx`:

```typescript
import { InboxItem } from "@/api/inbox";

interface Props {
  item: InboxItem;
  onAck: () => Promise<void>;
}

export function ProgressCheckinDetail({ item, onAck }: Props) {
  const iterations = (item.payload.iterations as number) ?? 0;
  const lastTool = (item.payload.last_tool as string) ?? "";
  const elapsed = (item.payload.elapsed_seconds as number) ?? 0;

  return (
    <div className="flex flex-col gap-3 p-6">
      <p className="text-sm whitespace-pre-wrap">{item.body ?? ""}</p>
      <ul className="text-xs text-muted-foreground space-y-1">
        <li>Iterations: {iterations}</li>
        <li>Last tool: {lastTool || "—"}</li>
        <li>Elapsed: {Math.round(elapsed / 60)} min</li>
      </ul>
      {item.status === "pending" && (
        <button
          className="self-start rounded bg-primary px-4 py-2 text-primary-foreground"
          onClick={onAck}
        >
          Acknowledge
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Create the dispatcher**

Create `web/src/features/inbox/inbox-detail.tsx`:

```typescript
import { useEffect, useState } from "react";

import {
  InboxItem,
  ackItem,
  getInboxItem,
  markRead,
  respondGovernance,
} from "@/api/inbox";
import { submitClarifyResponse } from "@/api/clarify";

import { GovernanceGateDetail } from "./detail/governance-gate";
import { InputRequiredDetail } from "./detail/input-required";
import { ProgressCheckinDetail } from "./detail/progress-checkin";
import { TaskCompleteDetail } from "./detail/task-complete";

interface Props {
  itemId: number;
  onChanged: () => void;
}

export function InboxDetail({ itemId, onChanged }: Props) {
  const [item, setItem] = useState<InboxItem | null>(null);

  useEffect(() => {
    let cancelled = false;
    getInboxItem(itemId).then((it) => {
      if (cancelled) return;
      setItem(it);
      if (!it.read_at) {
        markRead(itemId).then(() => onChanged());
      }
    });
    return () => { cancelled = true; };
  }, [itemId, onChanged]);

  if (!item) return <div className="p-6 text-muted-foreground">Loading...</div>;

  async function handleClarify(answers: Record<string, string>) {
    if (!item) return;
    const toolCallId = item.payload.tool_call_id as string;
    const questions = (item.payload.questions as { prompt: string }[]) ?? [];
    await submitClarifyResponse(
      item.session_id,
      toolCallId,
      questions.map((q) => ({
        question: q.prompt,
        answer: answers[q.prompt] ?? "",
        is_other: false,
      })),
    );
    const updated = await getInboxItem(itemId);
    setItem(updated);
    onChanged();
  }

  async function handleAck() {
    await ackItem(itemId);
    const updated = await getInboxItem(itemId);
    setItem(updated);
    onChanged();
  }

  async function handleDecision(decision: "approve" | "reject") {
    await respondGovernance(itemId, decision);
    const updated = await getInboxItem(itemId);
    setItem(updated);
    onChanged();
  }

  switch (item.kind) {
    case "input_required":
      return <InputRequiredDetail item={item} onSubmit={handleClarify} />;
    case "task_complete":
      return <TaskCompleteDetail item={item} onAck={handleAck} />;
    case "governance_gate":
      return <GovernanceGateDetail item={item} onDecision={handleDecision} />;
    case "progress_checkin":
      return <ProgressCheckinDetail item={item} onAck={handleAck} />;
    default:
      return <div className="p-6">Unknown item kind.</div>;
  }
}
```

- [ ] **Step 6: Commit**

```bash
git add web/src/features/inbox/inbox-detail.tsx web/src/features/inbox/detail/
git commit -m "feat(web): add per-kind inbox detail views"
```

---

### Task 21: Inbox page + route registration

**Files:**
- Create: `web/src/features/inbox/inbox-page.tsx`
- Create: `web/src/app/routes/inbox.tsx`
- Modify: `web/src/app/router.tsx`

- [ ] **Step 1: Create the page**

Create `web/src/features/inbox/inbox-page.tsx`:

```typescript
import { useState, useCallback } from "react";

import { InboxList } from "./inbox-list";
import { InboxDetail } from "./inbox-detail";
import { useInboxStream } from "./use-inbox-stream";

export function InboxPage() {
  const [selectedId, setSelectedId] = useState<number | undefined>(undefined);
  const [listKey, setListKey] = useState(0);

  const refreshList = useCallback(() => setListKey((k) => k + 1), []);

  useInboxStream(() => refreshList());

  return (
    <div className="flex h-full">
      <aside className="w-96 border-r overflow-y-auto" key={listKey}>
        <InboxList selectedId={selectedId} onSelect={setSelectedId} />
      </aside>
      <main className="flex-1 overflow-y-auto">
        {selectedId ? (
          <InboxDetail itemId={selectedId} onChanged={refreshList} />
        ) : (
          <div className="flex h-full items-center justify-center text-muted-foreground">
            Select an item.
          </div>
        )}
      </main>
    </div>
  );
}
```

- [ ] **Step 2: Register the route**

In the TanStack route tree (likely `web/src/app/routes/inbox.tsx`), add:

```typescript
import { createRoute } from "@tanstack/react-router";
import { InboxPage } from "@/features/inbox/inbox-page";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/inbox",
  beforeLoad: () => requireAuth(),
  component: InboxPage,
});
```

In `web/src/app/router.tsx`, import the new route and add it to `routeTree`:

```typescript
import { Route as inboxRoute } from "./routes/inbox";

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  linkRoute,
  settingsRoute,
  skillsRoute,
  agentsRoute,
  inboxRoute,
  chatRoute.addChildren([chatSessionRoute]),
]);
```

- [ ] **Step 3: Commit**

```bash
git add web/src/features/inbox/inbox-page.tsx web/src/app/routes/inbox.tsx web/src/app/router.tsx
git commit -m "feat(web): add /inbox route and page"
```

---

### Task 22: Navbar item + unread badge

**Files:**
- Modify: the existing navbar/sidebar component (find via `grep -rn 'NavBar\|Sidebar' web/src/`)

- [ ] **Step 1: Find the navbar**

Run: `grep -rn 'navbar\|sidebar\|nav-' /work/surogates2/surogates/web/src/components/ | head -10`

- [ ] **Step 2: Add the Inbox nav item with badge**

Edit the navbar component to add an Inbox link and a badge wired to `useInboxStore`:

```typescript
import { Link } from "@tanstack/react-router";
import { InboxIcon } from "lucide-react";

import { useInboxStore } from "@/stores/inbox-store";

function NavInbox() {
  const unread = useInboxStore((s) => s.unreadCount);
  return (
    <Link
      to="/inbox"
      className="relative flex items-center gap-2 px-3 py-2 rounded hover:bg-accent"
    >
      <InboxIcon size={18} />
      <span>Inbox</span>
      {unread > 0 && (
        <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-primary px-1 text-xs text-primary-foreground">
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </Link>
  );
}
```

Wire `<NavInbox />` into the navbar's items list. Also call `useInboxStream()` once at the top of the app shell (e.g. in the root layout) so the badge updates without requiring the user to open the inbox page first.

- [ ] **Step 3: Commit**

```bash
git add web/src/components/<navbar-file>
git commit -m "feat(web): add Inbox nav item with live unread badge"
```

---

## Phase 6 — End-to-end integration test

### Task 23: Full clarify path E2E test

**Files:**
- Create: `tests/integration/test_inbox_clarify_e2e.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: agent emits clarify, inbox item appears, user replies via
inbox endpoint, status flips to responded, harness sees the response."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_clarify_through_inbox(
    api_client, session_store, sample_session_with_user, run_clarify_tool
):
    from surogates.session.events import EventType

    # Agent emits clarify-equivalent.
    await run_clarify_tool(
        session_id=sample_session_with_user.id,
        tool_call_id="tc-e2e",
        questions=[{"prompt": "Pick a color"}],
        context="",
    )

    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}
    resp = await api_client.get("/v1/inbox", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["kind"] == "input_required"
    item_id = items[0]["id"]

    # User responds via the existing clarify endpoint (the inbox UI uses
    # the deep-linked clarify endpoint).
    resp = await api_client.post(
        f"/v1/sessions/{sample_session_with_user.id}/clarify/tc-e2e/respond",
        json={"responses": [{"question": "Pick a color", "answer": "blue"}]},
        headers=headers,
    )
    assert resp.status_code == 201

    # The inbox item is now responded.
    resp = await api_client.get(f"/v1/inbox/{item_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "responded"
```

- [ ] **Step 2: Run — verify it passes**

Run: `pytest tests/integration/test_inbox_clarify_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_inbox_clarify_e2e.py
git commit -m "test(inbox): clarify path end-to-end via inbox"
```

---

### Task 24: Governance + completion E2E tests

**Files:**
- Create: `tests/integration/test_inbox_governance_e2e.py`
- Create: `tests/integration/test_inbox_completion_e2e.py`

- [ ] **Step 1: Governance E2E**

```python
"""End-to-end: overridable denial → inbox item → user approves → session
wakes with USER_MESSAGE recording the decision."""

import pytest


@pytest.mark.asyncio
async def test_governance_approval_wakes_session(
    api_client, session_store, sample_session_with_user, tool_exec_for, monkeypatch
):
    from surogates.governance.policy import PolicyDecision
    from surogates.db.models import InboxItem, Event
    from sqlalchemy import select

    def fake_check(*a, **kw):
        return PolicyDecision(
            allowed=False, reason="external", tool_name="read_file", overridable=True,
            policy_id="external-comms",
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )
    woken = []
    async def fake_wake(request, sid): woken.append(sid)
    monkeypatch.setattr("surogates.api.routes.inbox._wake_session_from_request", fake_wake)

    await tool_exec_for(
        session_id=sample_session_with_user.id,
        tool_name="read_file",
        tool_call_id="tc-e2e-gov",
        arguments={"path": "/tmp/outside-workspace.txt"},
        session_config={"workspace_path": "/workspace"},
    )

    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}
    resp = await api_client.get("/v1/inbox?kind=governance_gate", headers=headers)
    item_id = resp.json()["items"][0]["id"]

    resp = await api_client.post(
        f"/v1/inbox/{item_id}/respond",
        json={"decision": "approve"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert sample_session_with_user.id in woken

    async with session_store._sf() as db:
        msgs = (await db.execute(
            select(Event).where(
                Event.session_id == sample_session_with_user.id,
                Event.type == "user.message",
            )
        )).scalars().all()
    assert any("APPROVE" in m.data.get("content", "") for m in msgs)
```

- [ ] **Step 2: Completion E2E**

```python
"""End-to-end: session completes → inbox item appears → user acks."""

import pytest


@pytest.mark.asyncio
async def test_completion_inbox_and_ack(
    api_client, session_store, sample_session_with_user, sample_lease, harness_for_session_with_user
):
    await harness_for_session_with_user._complete_session(
        sample_session_with_user,
        [{"role": "assistant", "content": "All done."}],
        sample_lease,
        reason="done",
    )

    headers = {"Authorization": f"Bearer {sample_session_with_user.user_jwt}"}
    resp = await api_client.get("/v1/inbox", headers=headers)
    item_id = resp.json()["items"][0]["id"]

    resp = await api_client.post(f"/v1/inbox/{item_id}/ack", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "acknowledged"
```

- [ ] **Step 3: Run all integration tests**

Run: `pytest tests/integration/ -v -k inbox`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_inbox_governance_e2e.py tests/integration/test_inbox_completion_e2e.py
git commit -m "test(inbox): governance and completion end-to-end paths"
```

---

## Phase 7 — Final verification

### Task 25: Full suite and lint

- [ ] **Step 1: Run the whole test suite**

Run: `pytest -q 2>&1 | tail -20`
Expected: all green (or no new failures vs. Task 0's baseline).

- [ ] **Step 2: Run any linter the project uses**

Run: `ruff check . 2>&1 | tail -20`
Expected: clean.

If `mypy` is configured: `mypy surogates/ 2>&1 | tail -20`.

- [ ] **Step 3: Frontend lint/typecheck**

In `web/`:

Run: `cd web && npm run lint && npm run typecheck 2>&1 | tail -20`
Expected: clean.

If `npm run build` is the project's gate:

Run: `cd web && npm run build 2>&1 | tail -10`
Expected: build succeeds; produces `web/dist/`.

- [ ] **Step 4: Manual smoke check**

Spin up a dev environment (`uvicorn surogates.api.app:app --reload` or however the project runs locally), open the web UI, create a clarify-emitting situation, verify the inbox badge increments and the detail view renders with a working reply form.

- [ ] **Step 5: Final commit (only if any lint/typecheck fixes were needed)**

```bash
git add -A
git commit -m "chore(inbox): lint + typecheck cleanup"
```

---

## Out of scope for this plan (per spec §3, §12)

- Email / Slack DM / Telegram bot push dispatchers (separate follow-up specs).
- A `user_notification_prefs` table.
- Cross-agent aggregation in surogate-ops.
- Archive / delete / bulk actions in the UI.
- Anonymous (website-channel) inbox surfacing.
- Service-account inbox (returns 403 by design).

## Verification checklist before declaring done

- [ ] All four `EventType` values exist and round-trip through `emit_event`.
- [ ] Anonymous and service-account sessions never write inbox rows (verified by tests).
- [ ] `inbox_items` rows always have `org_id`, `user_id`, `session_id`, `source_event_id`; unique constraint on `source_event_id` enforced.
- [ ] Clarify path: inbox item appears, web reply flips status to `responded`.
- [ ] Task-complete path: inbox item appears with outcome, ack flips status.
- [ ] Governance path: overridable denial → inbox item → approve emits `USER_MESSAGE`, wakes session, no auto-replay of the original tool call.
- [ ] Progress check-in path: respects `inbox_checkin_interval_seconds`, skipped when unset.
- [ ] Sweeper expires `pending` items for terminal sessions, leaves active sessions alone.
- [ ] Web inbox pane: list, per-kind detail, inline actions, live-update via SSE, navbar badge.
- [ ] Multi-tenancy: user A cannot read, ack, or respond to user B's items.
- [ ] Full test suite green.
