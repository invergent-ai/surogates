# Subagent Task Layer v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, DAG-aware subagent tasks with retry/block semantics on top of Surogates's existing `spawn_worker` / `delegate_task` infrastructure.

**Architecture:** New `surogates/tasks/` package holds the data model, four new tools (`spawn_task`, `unblock_task`, `cancel_task`, `task_block`), and a 5s tick loop hosted in the orchestrator dispatcher. Wraps existing `create_child_session` + `enqueue_session` primitives; does not replace `spawn_worker`/`delegate_task`.

**Tech Stack:** Python 3.12, async SQLAlchemy 2.x (Postgres), pytest + pytest-asyncio, Redis (sorted-set work queue + pub/sub interrupts).

**Reference spec:** [`/work/surogates/docs/sub-agents/2026-05-16-subagent-task-layer-v1.md`](./2026-05-16-subagent-task-layer-v1.md)

---

## Implementation Progress

Update this list before each commit. Status legend: `[ ]` not started · `[~]` in progress · `[x]` complete.

- [x] **Task 1**: SQLAlchemy schema — `tasks`, `task_links`, `sessions.task_id` + retrofit DDL
- [ ] **Task 2**: Pydantic models + new event types (`TASK_BLOCKED`, `TASK_FAILED`)
- [ ] **Task 3**: Factor `_create_session_for_task` primitive + extend `create_child_session`/`create_session` with `task_id`
- [ ] **Task 4**: `spawn_task` tool (eager spawn when ready, DAG validation)
- [ ] **Task 5**: `unblock_task` and `cancel_task` tool handlers
- [ ] **Task 6**: `task_block` self-tool
- [ ] **Task 7**: Tool registration + gating (`WORKER_EXCLUDED_TOOLS`, `_AGENT_TYPE_GATED_TOOLS`, `_filter_effective_tools`)
- [ ] **Task 8**: `WORKER_COMPLETE` payload includes `task_id`
- [ ] **Task 9**: `tasks_tick` — promote, finalize, enqueue
- [ ] **Task 10**: Wire `tasks_tick` into orchestrator + end-to-end integration tests

**Test placement decision** (added at execution time): DB-backed tests live under `tests/integration/tasks/` to inherit testcontainers fixtures (`engine`, `session_factory`, `session_store`, `redis_client`). Pure mock-based tests live under `tests/tasks/`. Adjust commit messages and `pytest` invocations accordingly.

---

## File Map

**New files:**
- `surogates/tasks/__init__.py` — package init, exports
- `surogates/tasks/models.py` — Pydantic `Task` domain model
- `surogates/tasks/spawn.py` — `_create_session_for_task` primitive (factored from `coordinator.py`)
- `surogates/tasks/tools.py` — 4 tool handlers + schemas + `register()`
- `surogates/tasks/dispatcher.py` — `tasks_tick()` with promote/finalize/enqueue
- `surogates/tasks/completion.py` — helpers used by dispatcher (event inspection)
- `tests/tasks/__init__.py`
- `tests/tasks/conftest.py` — shared helpers (`_make_task`, etc.) + DB fixtures for task ORM tests
- `tests/tasks/test_models.py`
- `tests/tasks/test_spawn.py`
- `tests/tasks/test_tools.py`
- `tests/tasks/test_dispatcher.py`
- `tests/tasks/test_integration.py`

**Modified files:**
- `surogates/db/models.py` — add SQLAlchemy `Task` + `TaskLink`; add `task_id` column on `Session`
- `surogates/db/observability.sql` — guarded retrofit DDL for existing deployments (`tasks`, `task_links`, `sessions.task_id`, indexes)
- `surogates/session/store.py` — add optional `task_id` parameter to `SessionStore.create_session`
- `surogates/session/provisioning.py` — add optional `task_id` parameter to `create_child_session` and pass through
- `surogates/session/models.py` — add `task_id` field on Pydantic `Session`
- `surogates/session/events.py` — add `TASK_BLOCKED`, `TASK_FAILED` to `EventType`
- `surogates/harness/worker_notify.py` — include `task_id` in `WORKER_COMPLETE` payload when set
- `surogates/orchestrator/worker.py` — `_filter_effective_tools` discards `task_block` when `session.task_id is None`
- `surogates/orchestrator/dispatcher.py` — run `tasks_tick()` every 5s alongside orphan sweep
- `surogates/harness/tool_schemas.py` — add `"spawn_task"` to `_AGENT_TYPE_GATED_TOOLS`
- `surogates/tools/builtin/coordinator.py` — extend `WORKER_EXCLUDED_TOOLS`; factor spawn helpers to be reusable
- `surogates/tools/builtin/__init__.py` — register `surogates.tasks.tools`

---

## Task 1: SQLAlchemy schema — `tasks`, `task_links`, `sessions.task_id`

**Files:**
- Modify: `surogates/db/models.py` (add at end, before any closing markers)
- Modify: `surogates/db/observability.sql` (guarded retrofit DDL for existing DBs)
- Create: `tests/tasks/conftest.py` (DB fixture aliases if not already available)
- Test: `tests/tasks/test_models.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/__init__.py` (empty) and `tests/tasks/test_models.py`:

```python
"""Schema tests for Task and TaskLink ORM models."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from surogates.db.models import Org, Session as ORMSession, Task, TaskLink


@pytest.mark.asyncio
async def test_task_round_trip(async_session_factory, seeded_org_id: uuid.UUID):
    """A Task row persists and round-trips with expected defaults."""
    parent_session_id = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=seeded_org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        await db.flush()
        task = Task(
            org_id=seeded_org_id,
            parent_session_id=parent_session_id,
            goal="research the postgres migration",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
    assert task.status == "todo"
    assert task.attempt_count == 0
    assert task.max_attempts == 3


@pytest.mark.asyncio
async def test_task_link_unique(async_session_factory, seeded_org_id: uuid.UUID):
    """task_links (parent_id, child_id) is the PK and rejects duplicates."""
    parent_session_id = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=seeded_org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        p = Task(org_id=seeded_org_id, parent_session_id=parent_session_id, goal="p")
        c = Task(org_id=seeded_org_id, parent_session_id=parent_session_id, goal="c")
        db.add_all([p, c])
        await db.flush()
        db.add(TaskLink(parent_id=p.id, child_id=c.id))
        await db.commit()

    async with async_session_factory() as db:
        db.add(TaskLink(parent_id=p.id, child_id=c.id))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_sessions_task_id_nullable_fk(async_session_factory, seeded_org_id: uuid.UUID):
    """sessions.task_id is nullable and FKs to tasks(id)."""
    parent_session_id = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(
            id=parent_session_id, org_id=seeded_org_id, agent_id="agent-a",
            channel="web", status="active",
        ))
        task = Task(org_id=seeded_org_id, parent_session_id=parent_session_id, goal="g")
        db.add(task)
        await db.flush()
        child = ORMSession(
            id=uuid.uuid4(), org_id=seeded_org_id, agent_id="agent-a",
            channel="task", status="active", task_id=task.id,
        )
        db.add(child)
        await db.commit()
        await db.refresh(child)
    assert child.task_id == task.id
```

The repo's root `tests/conftest.py` does not provide database fixtures, and `tests/integration/conftest.py` is not loaded for sibling `tests/tasks/` modules. Add `async_session_factory` and `seeded_org_id` to `tests/tasks/conftest.py` (or move these DB-backed tests under `tests/integration/tasks/` and update every command path consistently). If keeping `tests/tasks/`, reuse the same Testcontainers/engine pattern from `tests/integration/conftest.py` and add:

```python
import uuid

import pytest_asyncio

from surogates.db.models import Org


@pytest_asyncio.fixture(loop_scope="session")
async def async_session_factory(session_factory):
    return session_factory


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_org_id(async_session_factory):
    org_id = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(Org(id=org_id, name="task-test", config={}))
        await db.commit()
    return org_id
```

- [ ] **Step 2: Run integration tests after Task 9**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_models.py -v
```

Expected: FAIL with `ImportError: cannot import name 'Task' from 'surogates.db.models'` (or `TaskLink`).

- [ ] **Step 3: Add the models to `surogates/db/models.py`**

Find the end of the existing model definitions (after `SessionLease`/`SessionCursor` blocks) and append. Also locate the `Session` class (around line 162) and add a `task_id` column after `parent_id` (around line 211).

In the `Session` class, after the `parent_id` mapped_column:

```python
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
```

Append to the file (after the existing model definitions):

```python
# ---------------------------------------------------------------------------
# Subagent task layer
# ---------------------------------------------------------------------------


class Task(Base):
    """Durable subagent task — coordinates retries, DAG, and block/unblock
    around a goal that may be executed by zero or more Session attempts."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_org_status", "org_id", "status"),
        Index("idx_tasks_parent_session", "parent_session_id"),
        Index("idx_tasks_current_session", "current_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent_def_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="todo",
    )
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class TaskLink(Base):
    """Parent → child DAG edge between Tasks. Supports fan-in."""

    __tablename__ = "task_links"

    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True,
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True,
    )
```

- [ ] **Step 3b: Add retrofit DDL to `surogates/db/observability.sql`**

`Base.metadata.create_all` creates new tables on fresh databases, but it does **not** add `sessions.task_id` to an existing `sessions` table. Add guarded DDL near the existing "Sessions — retrofits" block:

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL REFERENCES orgs(id),
    parent_session_id  uuid NOT NULL REFERENCES sessions(id),
    agent_def_name     text,
    goal               text NOT NULL,
    context            text,
    current_session_id uuid REFERENCES sessions(id),
    status             text NOT NULL DEFAULT 'todo',
    result             text,
    blocked_reason     text,
    attempt_count      integer NOT NULL DEFAULT 0,
    max_attempts       integer NOT NULL DEFAULT 3,
    created_at         timestamp NOT NULL DEFAULT now(),
    started_at         timestamp,
    completed_at       timestamp
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id uuid NOT NULL REFERENCES tasks(id),
    child_id  uuid NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (parent_id, child_id)
);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS task_id uuid REFERENCES tasks(id);

CREATE INDEX IF NOT EXISTS idx_tasks_org_status
    ON tasks (org_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_session
    ON tasks (parent_session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_current_session
    ON tasks (current_session_id);
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_models.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/db/models.py surogates/db/observability.sql tests/tasks/__init__.py tests/tasks/conftest.py tests/tasks/test_models.py
git commit -m "feat(tasks): add Task and TaskLink ORM models with sessions.task_id"
```

---

## Task 2: Pydantic models + new event types

**Files:**
- Modify: `surogates/session/models.py:25-50` (add `task_id` to Pydantic Session)
- Modify: `surogates/session/events.py` (add `TASK_BLOCKED`, `TASK_FAILED` to `EventType`)
- Create: `surogates/tasks/__init__.py` (empty)
- Create: `surogates/tasks/models.py` (Pydantic `Task` mirror)
- Test: `tests/tasks/test_models.py` (extend existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_models.py`:

```python
def test_pydantic_task_constructible_from_orm():
    """Pydantic Task constructs from a SQLAlchemy Task row via from_attributes."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from surogates.tasks.models import Task as PydTask

    fake = type("FakeRow", (), {
        "id": _uuid.uuid4(),
        "org_id": _uuid.uuid4(),
        "parent_session_id": _uuid.uuid4(),
        "agent_def_name": None,
        "goal": "g",
        "context": None,
        "current_session_id": None,
        "status": "todo",
        "result": None,
        "blocked_reason": None,
        "attempt_count": 0,
        "max_attempts": 3,
        "created_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
    })()
    pyd = PydTask.model_validate(fake)
    assert pyd.status == "todo"
    assert pyd.attempt_count == 0


def test_pydantic_session_has_task_id_field():
    """Pydantic Session domain model carries task_id."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from surogates.session.models import Session as PydSession
    s = PydSession(
        id=_uuid.uuid4(), org_id=_uuid.uuid4(), agent_id="a",
        channel="task", status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        task_id=_uuid.uuid4(),
    )
    assert s.task_id is not None


def test_event_types_include_task_events():
    """EventType enum exposes TASK_BLOCKED and TASK_FAILED."""
    from surogates.session.events import EventType
    assert EventType.TASK_BLOCKED.value == "task.blocked"
    assert EventType.TASK_FAILED.value == "task.failed"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_models.py::test_pydantic_task_constructible_from_orm tests/tasks/test_models.py::test_pydantic_session_has_task_id_field tests/tasks/test_models.py::test_event_types_include_task_events -v
```

Expected: 3 FAIL with `ImportError` / `AttributeError`.

- [ ] **Step 3: Implement**

Create `surogates/tasks/__init__.py` (empty file).

Create `surogates/tasks/models.py`:

```python
"""Pydantic domain model for Task — mirrors the ORM Task row.

Used throughout the application layer; constructible from
``surogates.db.models.Task`` via ``model_config = {"from_attributes": True}``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


TaskStatus = Literal[
    "todo", "ready", "running", "blocked", "done", "failed", "cancelled",
]


class Task(BaseModel):
    """Snapshot of a Task row."""

    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    parent_session_id: UUID
    agent_def_name: str | None = None
    goal: str
    context: str | None = None
    current_session_id: UUID | None = None
    status: TaskStatus
    result: str | None = None
    blocked_reason: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

In `surogates/session/models.py`, locate the `Session(BaseModel)` class and add `task_id: UUID | None = None` after the existing `parent_id` field:

```python
    parent_id: UUID | None = None
    task_id: UUID | None = None   # NEW: set when this session is a Task attempt
    message_count: int = 0
```

In `surogates/session/events.py`, locate the `EventType` enum and add two members alongside the existing `WORKER_COMPLETE`:

```python
class EventType(Enum):
    # ... existing members ...
    WORKER_COMPLETE = "worker.complete"
    TASK_BLOCKED = "task.blocked"      # NEW
    TASK_FAILED = "task.failed"        # NEW
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_models.py -v
```

Expected: 6 tests PASS (the 3 from Task 1 plus 3 new).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/__init__.py surogates/tasks/models.py surogates/session/models.py surogates/session/events.py tests/tasks/test_models.py
git commit -m "feat(tasks): Pydantic Task model, sessions.task_id, TASK_BLOCKED/TASK_FAILED events"
```

---

## Task 3: Factor spawn primitive into `surogates/tasks/spawn.py`

**Goal:** Extract the lines 246-325 chunk from `_spawn_worker_handler` (in `tools/builtin/coordinator.py`) into a reusable `_build_worker_config()` + `_create_session_from_args()` so the new `spawn_task` tool and the dispatcher tick can both reuse it without duplication.

**Files:**
- Create: `surogates/tasks/spawn.py`
- Modify: `surogates/tools/builtin/coordinator.py:246-325` (refactor to call extracted helpers)
- Modify: `surogates/session/store.py` (`SessionStore.create_session(task_id=...)`)
- Modify: `surogates/session/provisioning.py` (`create_child_session(task_id=...)`)
- Test: `tests/tasks/test_spawn.py` (new), `tests/test_coordinator.py` (still passes unchanged)

- [ ] **Step 1: Write the failing test**

Append these shared helper functions to `tests/tasks/conftest.py` (created in Task 1 for DB fixtures):

```python
"""Shared test fixtures and helpers for the tasks package."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest


def _default_workspace_config() -> dict:
    return {
        "storage_bucket": "tenant-bucket",
        "workspace_path": "/workspace/tenant-bucket/parent",
        "supports_vision": False,
    }


def _make_session(**overrides: Any) -> MagicMock:
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.task_id = overrides.get("task_id")
    session.user_id = overrides.get("user_id")
    session.service_account_id = overrides.get("service_account_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", _default_workspace_config())
    return session


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.create_session = AsyncMock(return_value=_make_session(id=uuid4()))
    store.emit_event = AsyncMock(return_value=1)
    store.get_session = AsyncMock(return_value=_make_session())
    store.get_events = AsyncMock(return_value=[])
    return store


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    return redis


def _make_task(**overrides: Any) -> MagicMock:
    """A MagicMock ORM Task row with sensible defaults."""
    t = MagicMock()
    t.id = overrides.get("id", uuid4())
    t.org_id = overrides.get("org_id", uuid4())
    t.parent_session_id = overrides.get("parent_session_id", uuid4())
    t.agent_def_name = overrides.get("agent_def_name", None)
    t.goal = overrides.get("goal", "test goal")
    t.context = overrides.get("context", None)
    t.current_session_id = overrides.get("current_session_id", None)
    t.status = overrides.get("status", "ready")
    t.result = overrides.get("result", None)
    t.blocked_reason = overrides.get("blocked_reason", None)
    t.attempt_count = overrides.get("attempt_count", 0)
    t.max_attempts = overrides.get("max_attempts", 3)
    return t
```

Create `tests/tasks/test_spawn.py`:

```python
"""Tests for the factored spawn primitive."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from tests.tasks.conftest import _make_session, _make_store, _make_redis, _make_task


@pytest.mark.asyncio
async def test_create_session_for_task_sets_task_id_and_channel():
    """_create_session_for_task creates a child Session with task_id and channel='task'."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="research vLLM", context="for inference deployment")
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)
    store.create_session = AsyncMock(return_value=child)

    result = await _create_session_for_task(
        task,
        session_store=store,
    session_factory=None,
        tenant=MagicMock(org_id=task.org_id),
    )

    assert result.id == child.id
    # create_child_session ultimately calls create_session with channel="task"
    call_kwargs = store.create_session.call_args[1]
    assert call_kwargs["channel"] == "task"
    assert call_kwargs["parent_id"] == task.parent_session_id
    assert call_kwargs["task_id"] == task.id

    # USER_MESSAGE includes goal AND context block
    emit_calls = store.emit_event.call_args_list
    user_msg_calls = [c for c in emit_calls if c[0][1] == EventType.USER_MESSAGE]
    assert len(user_msg_calls) == 1
    msg_payload = user_msg_calls[0][0][2]
    assert "research vLLM" in msg_payload["content"]
    assert "for inference deployment" in msg_payload["content"]

    # WORKER_SPAWNED emitted on parent with task_id in payload
    spawn_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_SPAWNED]
    assert len(spawn_calls) == 1
    payload = spawn_calls[0][0][2]
    assert payload["task_id"] == str(task.id)
    assert payload["worker_id"] == str(child.id)


@pytest.mark.asyncio
async def test_create_session_for_task_no_context_just_goal():
    """When task.context is None, USER_MESSAGE is just the goal."""
    from surogates.tasks.spawn import _create_session_for_task

    task = _make_task(goal="just the goal", context=None)
    parent = _make_session(id=task.parent_session_id, agent_id="agent-1")
    child = _make_session(id=uuid4(), parent_id=task.parent_session_id)

    store = _make_store()
    store.get_session = AsyncMock(return_value=parent)
    store.create_session = AsyncMock(return_value=child)

    await _create_session_for_task(
        task, session_store=store, session_factory=None,
        tenant=MagicMock(org_id=task.org_id),
    )

    emit_calls = store.emit_event.call_args_list
    user_msg_calls = [c for c in emit_calls if c[0][1] == EventType.USER_MESSAGE]
    msg_payload = user_msg_calls[0][0][2]
    assert msg_payload["content"] == "just the goal"
    assert "## Context" not in msg_payload["content"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_spawn.py -v
```

Expected: 2 FAIL with `ImportError: cannot import name '_create_session_for_task' from 'surogates.tasks.spawn'`.

- [ ] **Step 3a: Extend session creation to carry `task_id`**

In `surogates/session/store.py`, add `task_id` to `SessionStore.create_session`:

```python
async def create_session(
    self,
    *,
    # ... existing parameters ...
    parent_id: UUID | None = None,
    task_id: UUID | None = None,
    service_account_id: UUID | None = None,
    # ...
) -> Session:
    row = SessionRow(
        # ... existing fields ...
        parent_id=parent_id,
        task_id=task_id,
        service_account_id=service_account_id,
        # ...
    )
```

In `surogates/session/provisioning.py`, add `task_id` to `create_child_session` and pass it through:

```python
async def create_child_session(
    *,
    store: SessionStore,
    parent: Session,
    channel: str,
    model: str | None = None,
    config: dict | None = None,
    service_account_id: UUID | None = None,
    idempotency_key: str | None = None,
    session_id: UUID | None = None,
    task_id: UUID | None = None,
) -> Session:
    # ... existing workspace-sharing logic ...
    return await store.create_session(
        # ... existing args ...
        parent_id=parent.id,
        task_id=task_id,
        service_account_id=effective_service_account_id,
        idempotency_key=idempotency_key,
    )
```

- [ ] **Step 3b: Implement `surogates/tasks/spawn.py`**

```python
"""Spawn primitive for task-backed worker sessions.

Factored from ``tools/builtin/coordinator.py:_spawn_worker_handler`` so the
spawn_task tool and the tasks_tick dispatcher can share the exact same
child-session-creation logic without duplication.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from surogates.session.provisioning import create_child_session
from surogates.session.events import EventType
from surogates.tools.builtin.coordinator import _WORKER_MAX_ITERATIONS, WORKER_EXCLUDED_TOOLS


def _build_task_worker_config(agent_def: Any | None, task: Any) -> dict[str, Any]:
    """Build the worker config dict for a task-backed session.

    Mirrors the config-building logic in _spawn_worker_handler but reads
    agent_def + iteration ceiling from the task row rather than tool args.
    """
    child_iterations = _WORKER_MAX_ITERATIONS
    if agent_def is not None and agent_def.max_iterations is not None:
        child_iterations = min(child_iterations, agent_def.max_iterations)

    cfg: dict[str, Any] = {
        "max_iterations": child_iterations,
        "streaming": False,
    }
    if task.agent_def_name:
        cfg["agent_type"] = task.agent_def_name
    if agent_def is not None and agent_def.policy_profile:
        cfg["policy_profile"] = agent_def.policy_profile
    if agent_def is not None and agent_def.tools:
        allowed = [t for t in agent_def.tools if t not in WORKER_EXCLUDED_TOOLS]
        cfg["allowed_tools"] = allowed
    else:
        cfg["excluded_tools"] = list(WORKER_EXCLUDED_TOOLS)
    if agent_def is not None and agent_def.disallowed_tools:
        existing = set(cfg.get("excluded_tools") or [])
        existing.update(agent_def.disallowed_tools)
        if "allowed_tools" not in cfg:
            cfg["excluded_tools"] = list(existing)
    return cfg


async def _create_session_for_task(
    task: Any,
    *,
    session_store: Any,
    session_factory: Any | None,
    tenant: Any,
) -> Any:
    """Create a child Session for *task*, emit USER_MESSAGE, emit WORKER_SPAWNED.

    Returns the child Session (caller is responsible for setting
    task.current_session_id and enqueueing).
    """
    parent = await session_store.get_session(task.parent_session_id)

    agent_def = None
    if task.agent_def_name:
        from surogates.harness.agent_resolver import resolve_agent_by_name
        agent_def = await resolve_agent_by_name(
            task.agent_def_name, tenant, session_factory=session_factory,
        )

    worker_config = _build_task_worker_config(agent_def, task)
    child = await create_child_session(
        store=session_store,
        parent=parent,
        channel="task",
        model=(agent_def.model if agent_def else None),
        config=worker_config,
        task_id=task.id,
    )

    user_msg = task.goal
    if task.context:
        user_msg = f"{task.goal}\n\n## Context\n{task.context}"
    await session_store.emit_event(
        child.id, EventType.USER_MESSAGE, {"content": user_msg},
    )
    await session_store.emit_event(
        task.parent_session_id,
        EventType.WORKER_SPAWNED,
        {
            "worker_id": str(child.id),
            "task_id": str(task.id),
            "goal": task.goal,
        },
    )
    return child
```

Do not bypass `create_child_session`: plain `SessionStore.create_session` will not copy the parent's `storage_bucket`, `workspace_path`, `supports_vision`, or sandbox root config.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_spawn.py -v
```

Expected: 2 tests PASS.

Also run the existing coordinator tests to confirm no regression:

```bash
cd /work/surogates && uv run pytest tests/test_coordinator.py -v
```

Expected: all pass (we didn't refactor `_spawn_worker_handler` itself yet — that's intentional, only the new code uses the factored helpers).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/spawn.py surogates/session/store.py surogates/session/provisioning.py tests/tasks/conftest.py tests/tasks/test_spawn.py
git commit -m "feat(tasks): factor _create_session_for_task primitive"
```

---

## Task 4: `spawn_task` tool — eager spawn when ready, deferred when todo

**Files:**
- Create: `surogates/tasks/tools.py` (begin — schemas + spawn_task only; other handlers in later tasks)
- Test: `tests/tasks/test_tools.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_tools.py`:

```python
"""Tests for spawn_task, unblock_task, cancel_task, task_block tool handlers."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tests.tasks.conftest import _make_session, _make_store, _make_redis


@pytest.mark.asyncio
async def test_spawn_task_no_parents_eager_spawns():
    """spawn_task with no parents returns status='running' and spawns a session."""
    from surogates.tasks.tools import _spawn_task_handler

    parent_id = uuid4()
    child_id = uuid4()
    org_id = uuid4()

    # Mock the DB layer — spawn_task uses an async session factory.
    db = AsyncMock()
    created_task = MagicMock(
        id=uuid4(), org_id=org_id, parent_session_id=parent_id,
        goal="g", context=None, status="ready", attempt_count=0,
        max_attempts=3, agent_def_name=None,
    )
    # The handler builds the Task, flushes, then potentially commits as 'running'.
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock(return_value=None)
    db.scalars = AsyncMock()  # not called when parents=[]

    session_factory = MagicMock(return_value=db)
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    store = _make_store()
    store.get_session = AsyncMock(return_value=_make_session(id=parent_id, agent_id="a"))
    store.create_session = AsyncMock(return_value=_make_session(id=child_id, parent_id=parent_id))

    redis = _make_redis()

    with patch("surogates.tasks.tools.Task", return_value=created_task):
        result = await _spawn_task_handler(
            {"goal": "g"},
            session_store=store,
            redis=redis,
            tenant=MagicMock(org_id=org_id),
            session_id=str(parent_id),
            session_factory=session_factory,
        )

    parsed = json.loads(result)
    assert parsed["status"] == "running"
    assert "task_id" in parsed


@pytest.mark.asyncio
async def test_spawn_task_rejects_missing_goal():
    """spawn_task with no goal returns a tool error."""
    from surogates.tasks.tools import _spawn_task_handler

    result = await _spawn_task_handler(
        {},
        session_store=_make_store(),
        redis=_make_redis(),
        tenant=MagicMock(org_id=uuid4()),
        session_id=str(uuid4()),
        session_factory=MagicMock(),
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_spawn_task_rejects_missing_parent():
    """spawn_task rejects parent task ids that do not exist."""
    from surogates.tasks.tools import _spawn_task_handler

    db = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    db.scalars = AsyncMock(return_value=scalars_result)

    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await _spawn_task_handler(
        {"goal": "g", "parents": [str(uuid4())]},  # non-existent parent
        session_store=_make_store(),
        redis=_make_redis(),
        tenant=MagicMock(org_id=uuid4()),
        session_id=str(uuid4()),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "parent" in parsed["error"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py -v
```

Expected: 3 FAIL with `ImportError: cannot import name '_spawn_task_handler'`.

- [ ] **Step 3: Implement `surogates/tasks/tools.py`** (initial — spawn_task only)

```python
"""Subagent task tools: spawn_task, unblock_task, cancel_task, task_block.

Adds DAG-aware durable task semantics on top of the existing spawn_worker
infrastructure. spawn_task creates a Task row; if all parents are done (or
none specified) it eagerly creates the child Session and enqueues it,
returning status='running'. Otherwise the Task waits in 'todo' until the
dispatcher tick promotes it.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from surogates.config import enqueue_session
from surogates.db.models import Task, TaskLink
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


_SPAWN_TASK_SCHEMA = ToolSchema(
    name="spawn_task",
    description=(
        "Spawn a durable subagent task that survives crashes, supports DAG "
        "dependencies, retries on failure, and can be paused/resumed via "
        "task_block/unblock_task. Use this when work needs to outlive a "
        "single LLM turn, fan in on multiple parents, or be inspectable "
        "by a human. Prefer spawn_worker for one-shot fire-and-forget."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Complete, self-contained description of the task. "
                    "The subagent has no access to your conversation."
                ),
            },
            "context": {
                "type": "string",
                "description": "Additional structured context for the subagent.",
            },
            "agent_type": {
                "type": "string",
                "description": (
                    "Optional name of a pre-configured sub-agent type from "
                    "the tenant catalog. Inherits system prompt, tool filter, "
                    "model, iteration cap."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Task ids this task depends on. Stays in 'todo' until "
                    "every parent reaches 'done'; then auto-promotes to 'ready'."
                ),
            },
            "max_attempts": {
                "type": "integer",
                "description": (
                    "Retry budget. Default 3. The dispatcher gives up after "
                    "this many consecutive crash/timeout attempts."
                ),
            },
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
)


def _tool_error(msg: str) -> str:
    return json.dumps({"error": msg})


async def _spawn_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Create a Task row; eagerly spawn child Session if no pending parents."""
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    tenant = kwargs.get("tenant")
    session_id_str = kwargs.get("session_id")
    session_factory = kwargs.get("session_factory")

    if not session_store or not tenant or not session_id_str or not session_factory:
        return _tool_error("required context not available")

    goal = arguments.get("goal")
    if not goal or not str(goal).strip():
        return _tool_error("goal is required")

    context = arguments.get("context")
    agent_def_name = arguments.get("agent_type")
    parents_raw = arguments.get("parents") or []
    max_attempts = arguments.get("max_attempts") or 3

    if not isinstance(parents_raw, list):
        return _tool_error("parents must be a list of task ids")

    try:
        parent_ids = [UUID(str(p)) for p in parents_raw]
    except (ValueError, TypeError):
        return _tool_error("invalid task id in parents")

    parent_session_id = UUID(str(session_id_str))
    org_id = tenant.org_id

    async with session_factory() as db:
        # Validate parents exist + are in same org. Compute initial status.
        initial_status = "ready"
        if parent_ids:
            parent_rows = (await db.scalars(
                select(Task).where(Task.id.in_(parent_ids))
            )).all()
            if len(parent_rows) != len(parent_ids):
                return _tool_error("one or more parents do not exist")
            for p in parent_rows:
                if p.org_id != org_id:
                    return _tool_error("parents must be in the same org")
            if any(p.status != "done" for p in parent_rows):
                initial_status = "todo"

        task = Task(
            org_id=org_id,
            parent_session_id=parent_session_id,
            agent_def_name=agent_def_name,
            goal=str(goal).strip(),
            context=context,
            status=initial_status,
            max_attempts=int(max_attempts),
        )
        db.add(task)
        await db.flush()  # populate task.id

        for pid in parent_ids:
            db.add(TaskLink(parent_id=pid, child_id=task.id))

        # Eager spawn for the no-pending-parents case.
        if initial_status == "ready":
            from surogates.tasks.spawn import _create_session_for_task
            child = await _create_session_for_task(
                task,
                session_store=session_store,
                session_factory=session_factory,
                tenant=tenant,
            )
            task.current_session_id = child.id
            task.status = "running"
            task.attempt_count = task.attempt_count + 1
            from sqlalchemy import func
            task.started_at = func.now()
            await db.commit()
            await enqueue_session(redis, child.agent_id, child.id)
            return json.dumps({
                "task_id": str(task.id),
                "status": "running",
                "worker_id": str(child.id),
            })

        await db.commit()
        return json.dumps({"task_id": str(task.id), "status": "todo"})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/tools.py tests/tasks/test_tools.py
git commit -m "feat(tasks): spawn_task tool with eager spawn and DAG validation"
```

---

## Task 5: `unblock_task` and `cancel_task` tool handlers

**Files:**
- Modify: `surogates/tasks/tools.py` (append)
- Test: `tests/tasks/test_tools.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_unblock_task_requires_ownership():
    """unblock_task refuses if caller is not the spawning parent session."""
    from surogates.tasks.tools import _unblock_task_handler

    task_id = uuid4()
    spawner_session_id = uuid4()
    other_session_id = uuid4()

    task = MagicMock(id=task_id, parent_session_id=spawner_session_id, status="blocked")
    db = AsyncMock()
    db.get = AsyncMock(return_value=task)
    db.commit = AsyncMock()

    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await _unblock_task_handler(
        {"task_id": str(task_id)},
        session_store=_make_store(),
        redis=_make_redis(),
        tenant=MagicMock(),
        session_id=str(other_session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "spawned" in parsed["error"].lower() or "own" in parsed["error"].lower()


@pytest.mark.asyncio
async def test_unblock_task_appends_additional_context():
    """unblock_task appends additional_context to task.context with a marker."""
    from surogates.tasks.tools import _unblock_task_handler

    task_id = uuid4()
    spawner_id = uuid4()
    task = MagicMock(
        id=task_id, parent_session_id=spawner_id,
        status="blocked", context="original",
    )

    db = AsyncMock()
    db.get = AsyncMock(return_value=task)
    db.commit = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await _unblock_task_handler(
        {"task_id": str(task_id), "additional_context": "the answer is X"},
        session_store=_make_store(),
        redis=_make_redis(),
        tenant=MagicMock(),
        session_id=str(spawner_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    assert "original" in task.context
    assert "the answer is X" in task.context
    assert task.status == "ready"
    assert task.blocked_reason is None


@pytest.mark.asyncio
async def test_cancel_task_interrupts_running():
    """cancel_task on a running task publishes to the interrupt channel."""
    from surogates.tasks.tools import _cancel_task_handler

    task_id = uuid4()
    spawner_id = uuid4()
    worker_session_id = uuid4()
    task = MagicMock(
        id=task_id, parent_session_id=spawner_id,
        status="running", current_session_id=worker_session_id,
    )

    db = AsyncMock()
    db.get = AsyncMock(return_value=task)
    db.commit = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    redis = _make_redis()
    result = await _cancel_task_handler(
        {"task_id": str(task_id)},
        session_store=_make_store(),
        redis=redis,
        tenant=MagicMock(),
        session_id=str(spawner_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    assert task.status == "cancelled"
    redis.publish.assert_called_once()
    channel_arg = redis.publish.call_args[0][0]
    assert str(worker_session_id) in channel_arg
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py::test_unblock_task_requires_ownership tests/tasks/test_tools.py::test_unblock_task_appends_additional_context tests/tasks/test_tools.py::test_cancel_task_interrupts_running -v
```

Expected: 3 FAIL with `ImportError`.

- [ ] **Step 3: Implement (append to `surogates/tasks/tools.py`)**

Append after `_spawn_task_handler`:

```python
_UNBLOCK_TASK_SCHEMA = ToolSchema(
    name="unblock_task",
    description=(
        "Resume a blocked subagent task. Only the spawning parent session "
        "can unblock its children. Optional additional_context is delivered "
        "as part of the next attempt's initial input, so the worker sees "
        "the new information you provided."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "additional_context": {
                "type": "string",
                "description": "Extra context to give the next attempt.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)


_CANCEL_TASK_SCHEMA = ToolSchema(
    name="cancel_task",
    description=(
        "Cancel a non-terminal subagent task. Only the spawning parent "
        "session can cancel its children. If the task is running, its "
        "current attempt session is interrupted via the standard stop "
        "mechanism."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)


async def _unblock_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_factory = kwargs.get("session_factory")
    session_id_str = kwargs.get("session_id")
    if not session_factory or not session_id_str:
        return _tool_error("required context not available")

    task_id_str = arguments.get("task_id")
    if not task_id_str:
        return _tool_error("task_id is required")
    try:
        task_id = UUID(str(task_id_str))
    except ValueError:
        return _tool_error("invalid task_id")

    additional = arguments.get("additional_context")
    parent_session_id = UUID(str(session_id_str))

    from datetime import datetime, timezone
    async with session_factory() as db:
        task = await db.get(Task, task_id, with_for_update=True)
        if task is None:
            return _tool_error(f"task {task_id} not found")
        if task.parent_session_id != parent_session_id:
            return _tool_error("can only unblock tasks you spawned")
        if task.status != "blocked":
            return _tool_error(f"task is not blocked (status={task.status})")
        task.status = "ready"
        task.blocked_reason = None
        if additional:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            prefix = task.context or ""
            task.context = (
                f"{prefix}\n\n[unblock at {stamp}]\n{additional}".strip()
            )
        await db.commit()
    return json.dumps({"ok": True, "task_id": str(task_id), "status": "ready"})


async def _cancel_task_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_factory = kwargs.get("session_factory")
    session_id_str = kwargs.get("session_id")
    redis = kwargs.get("redis")
    if not session_factory or not session_id_str or not redis:
        return _tool_error("required context not available")

    task_id_str = arguments.get("task_id")
    if not task_id_str:
        return _tool_error("task_id is required")
    try:
        task_id = UUID(str(task_id_str))
    except ValueError:
        return _tool_error("invalid task_id")

    parent_session_id = UUID(str(session_id_str))
    from sqlalchemy import func
    from surogates.config import INTERRUPT_CHANNEL_PREFIX

    async with session_factory() as db:
        task = await db.get(Task, task_id, with_for_update=True)
        if task is None:
            return _tool_error(f"task {task_id} not found")
        if task.parent_session_id != parent_session_id:
            return _tool_error("can only cancel tasks you spawned")
        if task.status in ("done", "failed", "cancelled"):
            return _tool_error(f"task already terminal: {task.status}")

        running_session_id = task.current_session_id if task.status == "running" else None
        task.status = "cancelled"
        task.completed_at = func.now()
        await db.commit()

    if running_session_id is not None:
        await redis.publish(
            f"{INTERRUPT_CHANNEL_PREFIX}:{running_session_id}", "task_cancel",
        )
    return json.dumps({"ok": True, "task_id": str(task_id), "status": "cancelled"})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py -v
```

Expected: 6 tests PASS (3 from Task 4 + 3 new).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/tools.py tests/tasks/test_tools.py
git commit -m "feat(tasks): unblock_task and cancel_task tools with ownership checks"
```

---

## Task 6: `task_block` self-tool

**Files:**
- Modify: `surogates/tasks/tools.py` (append)
- Test: `tests/tasks/test_tools.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_task_block_only_when_running_for_task():
    """task_block refuses if the calling Session has no task_id."""
    from surogates.tasks.tools import _task_block_handler

    session_id = uuid4()
    # Session without a task_id
    pyd_session = MagicMock(task_id=None)
    db = AsyncMock()
    db.get = AsyncMock(return_value=pyd_session)
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await _task_block_handler(
        {"reason": "need an answer"},
        session_store=_make_store(),
        redis=_make_redis(),
        tenant=MagicMock(),
        session_id=str(session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_task_block_marks_blocked_and_publishes_interrupt():
    """task_block sets task to blocked, emits TASK_BLOCKED, publishes interrupt."""
    from surogates.session.events import EventType
    from surogates.tasks.tools import _task_block_handler

    session_id = uuid4()
    task_id = uuid4()
    parent_id = uuid4()

    session_row = MagicMock(id=session_id, task_id=task_id)
    task = MagicMock(
        id=task_id, parent_session_id=parent_id,
        current_session_id=session_id, status="running",
    )

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[session_row, task])
    db.commit = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    redis = _make_redis()
    store = _make_store()

    result = await _task_block_handler(
        {"reason": "rate limit key unclear"},
        session_store=store,
        redis=redis,
        tenant=MagicMock(),
        session_id=str(session_id),
        session_factory=session_factory,
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True
    assert task.status == "blocked"
    assert task.blocked_reason == "rate limit key unclear"

    # TASK_BLOCKED emitted to parent
    emit_calls = store.emit_event.call_args_list
    blocked_calls = [c for c in emit_calls if c[0][1] == EventType.TASK_BLOCKED]
    assert len(blocked_calls) == 1
    assert blocked_calls[0][0][0] == parent_id

    # Interrupt published on this session's channel
    redis.publish.assert_called_once()
    assert str(session_id) in redis.publish.call_args[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py::test_task_block_only_when_running_for_task tests/tasks/test_tools.py::test_task_block_marks_blocked_and_publishes_interrupt -v
```

Expected: 2 FAIL with `ImportError`.

- [ ] **Step 3: Implement (append to `surogates/tasks/tools.py`)**

```python
_TASK_BLOCK_SCHEMA = ToolSchema(
    name="task_block",
    description=(
        "Pause your own task and wait for additional context. Only "
        "available when your session is executing a task (set by the "
        "dispatcher). Provide a one-sentence reason naming the specific "
        "decision you need; deeper context belongs in your messages. "
        "Does NOT consume a retry attempt — blocking is a deliberate "
        "pause, not a failure."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
)


async def _task_block_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    session_id_str = kwargs.get("session_id")
    session_factory = kwargs.get("session_factory")
    if not session_store or not redis or not session_id_str or not session_factory:
        return _tool_error("required context not available")

    reason = arguments.get("reason")
    if not reason or not str(reason).strip():
        return _tool_error("reason is required — explain what input you need")

    session_id = UUID(str(session_id_str))

    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.db.models import Session as ORMSession
    from surogates.session.events import EventType

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        if sess is None or sess.task_id is None:
            return _tool_error("not running for a task")

        task = await db.get(Task, sess.task_id, with_for_update=True)
        if task is None or task.current_session_id != session_id:
            return _tool_error("task is no longer claimed by this session")
        if task.status != "running":
            return _tool_error(f"task is not running (status={task.status})")

        task.status = "blocked"
        task.blocked_reason = str(reason).strip()
        parent_session_id = task.parent_session_id
        task_id = task.id
        await db.commit()

    await session_store.emit_event(
        parent_session_id, EventType.TASK_BLOCKED,
        {
            "task_id": str(task_id),
            "worker_id": str(session_id),
            "reason": str(reason).strip(),
        },
    )
    await redis.publish(
        f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}", "task_block",
    )
    return json.dumps({"ok": True, "task_id": str(task_id), "status": "blocked"})
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/tools.py tests/tasks/test_tools.py
git commit -m "feat(tasks): task_block self-tool with parent event + interrupt"
```

---

## Task 7: Tool registration + gating

**Files:**
- Modify: `surogates/tasks/tools.py` (add `register()` function)
- Modify: `surogates/tools/builtin/__init__.py` or wherever core tools are registered (locate by grep)
- Modify: `surogates/tools/builtin/coordinator.py` (extend `WORKER_EXCLUDED_TOOLS`)
- Modify: `surogates/orchestrator/worker.py:686-702` (gate `task_block` on `session.task_id`)
- Modify: `surogates/harness/tool_schemas.py` (add `"spawn_task"` to `_AGENT_TYPE_GATED_TOOLS`)
- Test: `tests/tasks/test_tools.py` (registration tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_tools.py`:

```python
def test_register_adds_all_four_tools():
    """tasks.tools.register adds the 4 tools to the registry."""
    from surogates.tools.registry import ToolRegistry
    from surogates.tasks.tools import register

    reg = ToolRegistry()
    register(reg)
    names = reg.tool_names
    assert "spawn_task" in names
    assert "unblock_task" in names
    assert "cancel_task" in names
    assert "task_block" in names


def test_worker_excluded_tools_extends_to_task_tools():
    """Children spawned via spawn_worker can't recursively spawn tasks."""
    from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS
    assert "spawn_task" in WORKER_EXCLUDED_TOOLS
    assert "unblock_task" in WORKER_EXCLUDED_TOOLS
    assert "cancel_task" in WORKER_EXCLUDED_TOOLS
    # spawn_worker etc still in the set
    assert "spawn_worker" in WORKER_EXCLUDED_TOOLS


def test_agent_type_gated_includes_spawn_task():
    """spawn_task's agent_type param is stripped when tenant has no AgentDefs."""
    from surogates.harness.tool_schemas import _AGENT_TYPE_GATED_TOOLS
    assert "spawn_task" in _AGENT_TYPE_GATED_TOOLS
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py::test_register_adds_all_four_tools tests/tasks/test_tools.py::test_worker_excluded_tools_extends_to_task_tools tests/tasks/test_tools.py::test_agent_type_gated_includes_spawn_task -v
```

Expected: 3 FAIL.

- [ ] **Step 3: Implement**

Append to `surogates/tasks/tools.py`:

```python
def register(registry: ToolRegistry) -> None:
    """Register the four task-layer tools."""
    registry.register(
        name="spawn_task", schema=_SPAWN_TASK_SCHEMA,
        handler=_spawn_task_handler, toolset="core",
    )
    registry.register(
        name="unblock_task", schema=_UNBLOCK_TASK_SCHEMA,
        handler=_unblock_task_handler, toolset="core",
    )
    registry.register(
        name="cancel_task", schema=_CANCEL_TASK_SCHEMA,
        handler=_cancel_task_handler, toolset="core",
    )
    registry.register(
        name="task_block", schema=_TASK_BLOCK_SCHEMA,
        handler=_task_block_handler, toolset="core",
    )
```

In `surogates/tools/builtin/coordinator.py:42-46`, extend `WORKER_EXCLUDED_TOOLS`:

```python
WORKER_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "spawn_worker",
    "send_worker_message",
    "stop_worker",
    "spawn_task",      # NEW: children can't recursively spawn task layer
    "unblock_task",    # NEW
    "cancel_task",     # NEW
})
```

In `surogates/harness/tool_schemas.py:15-18`, extend `_AGENT_TYPE_GATED_TOOLS`:

```python
_AGENT_TYPE_GATED_TOOLS: frozenset[str] = frozenset({
    "delegate_task",
    "spawn_worker",
    "spawn_task",     # NEW
})
```

In `surogates/orchestrator/worker.py`, locate `_filter_effective_tools` (around line 686-702). After the existing filtering and just before returning, add:

```python
    # Gate task_block: it only makes sense for sessions executing a task.
    if getattr(session, "task_id", None) is None:
        result.discard("task_block")
```

Find where the existing tools are registered (search for "coordinator.register" or "delegate.register"). Add the task tools registration alongside. Typically `surogates/tools/builtin/__init__.py` or wherever the registry is bootstrapped:

```python
# Locate the existing call pattern, e.g.:
#   from surogates.tools.builtin import coordinator
#   coordinator.register(registry)
# Add:
from surogates.tasks import tools as tasks_tools
tasks_tools.register(registry)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_tools.py tests/test_coordinator.py -v
```

Expected: 11 tests PASS in `test_tools.py`; all existing `test_coordinator.py` tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/tools.py surogates/tools/builtin/coordinator.py surogates/harness/tool_schemas.py surogates/orchestrator/worker.py surogates/tools/builtin/__init__.py tests/tasks/test_tools.py
git commit -m "feat(tasks): register task tools and extend gating"
```

---

## Task 8: `WORKER_COMPLETE` payload extension

**Files:**
- Modify: `surogates/harness/worker_notify.py` (around line 41-57)
- Test: `tests/test_coordinator.py` (extend) or `tests/tasks/test_completion.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_completion.py`:

```python
"""Tests for WORKER_COMPLETE payload extension to include task_id."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_worker_complete_payload_includes_task_id_when_set():
    """When the completing session has task_id, the payload carries it."""
    from surogates.harness.worker_notify import notify_parent_on_completion
    from surogates.session.events import EventType

    parent_id = uuid4()
    worker_id = uuid4()
    task_id = uuid4()

    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.emit_event = AsyncMock(return_value=1)

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_completion(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="a",
        redis=redis,
        task_id=task_id,
    )

    emit_calls = store.emit_event.call_args_list
    complete_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_COMPLETE]
    assert len(complete_calls) >= 1
    payload = complete_calls[0][0][2]
    assert payload.get("task_id") == str(task_id)


@pytest.mark.asyncio
async def test_worker_complete_payload_omits_task_id_when_none():
    """A plain spawn_worker session has no task_id; payload omits the key."""
    from surogates.harness.worker_notify import notify_parent_on_completion
    from surogates.session.events import EventType

    parent_id = uuid4()
    worker_id = uuid4()

    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.emit_event = AsyncMock(return_value=1)

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    await notify_parent_on_completion(
        session_store=store,
        worker_session_id=worker_id,
        parent_session_id=parent_id,
        agent_id="a",
        redis=redis,
        task_id=None,
    )

    emit_calls = store.emit_event.call_args_list
    complete_calls = [c for c in emit_calls if c[0][1] == EventType.WORKER_COMPLETE]
    payload = complete_calls[0][0][2]
    assert "task_id" not in payload or payload["task_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_completion.py -v
```

Expected: 2 FAIL (payload missing `task_id` field, or wrong import name).

- [ ] **Step 3: Implement — modify `worker_notify.py`**

Open `surogates/harness/worker_notify.py` and locate `notify_parent_on_completion`. Extend the signature with `task_id: UUID | None = None` and include it in the emitted `WORKER_COMPLETE` payload:

```python
async def notify_parent_on_completion(
    *,
    session_store: SessionStore,
    worker_session_id: UUID,
    parent_session_id: UUID,
    agent_id: str,
    redis: Redis | None = None,
    task_id: UUID | None = None,
) -> None:
    # ...
    payload = {
        "worker_id": str(worker_session_id),
        "result": final_response[:_MAX_RESULT_CHARS],
    }
    if task_id is not None:
        payload["task_id"] = str(task_id)
    await session_store.emit_event(
        parent_session_id,
        EventType.WORKER_COMPLETE,
        payload,
    )
```

Then update the call in `surogates/harness/loop.py:_complete_session` to pass the Pydantic session's new `task_id` field:

```python
await notify_parent_on_completion(
    session_store=self._store,
    worker_session_id=session.id,
    parent_session_id=session.parent_id,
    agent_id=session.agent_id,
    redis=self._redis,
    task_id=getattr(session, "task_id", None),
)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_completion.py tests/test_coordinator.py -v
```

Expected: 2 new tests PASS; existing coordinator tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/harness/worker_notify.py surogates/harness/loop.py tests/tasks/test_completion.py
git commit -m "feat(tasks): include task_id in WORKER_COMPLETE payload"
```

---

## Task 9: `tasks_tick` — promote, finalize, enqueue

**Files:**
- Create: `surogates/tasks/dispatcher.py`
- Test: `tests/tasks/test_dispatcher.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_dispatcher.py`:

```python
"""Tests for tasks_tick: promote todo→ready, finalize ended sessions, enqueue ready."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from surogates.db.models import Org, Session as ORMSession, Task, TaskLink


@pytest.mark.asyncio
async def test_promote_promotes_todo_when_all_parents_done(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    from surogates.tasks.dispatcher import _promote_todo_to_ready

    sp = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        p1 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="p1", status="done")
        p2 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="p2", status="done")
        c = Task(org_id=seeded_org_id, parent_session_id=sp, goal="c", status="todo")
        db.add_all([p1, p2, c])
        await db.flush()
        db.add_all([TaskLink(parent_id=p1.id, child_id=c.id),
                    TaskLink(parent_id=p2.id, child_id=c.id)])
        await db.commit()
        cid = c.id

    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        promoted = await db.get(Task, cid)
        assert promoted.status == "ready"


@pytest.mark.asyncio
async def test_promote_keeps_todo_when_any_parent_unfinished(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    from surogates.tasks.dispatcher import _promote_todo_to_ready

    sp = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        p1 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="p1", status="done")
        p2 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="p2", status="running")
        c = Task(org_id=seeded_org_id, parent_session_id=sp, goal="c", status="todo")
        db.add_all([p1, p2, c])
        await db.flush()
        db.add_all([TaskLink(parent_id=p1.id, child_id=c.id),
                    TaskLink(parent_id=p2.id, child_id=c.id)])
        await db.commit()
        cid = c.id

    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        unchanged = await db.get(Task, cid)
        assert unchanged.status == "todo"


@pytest.mark.asyncio
async def test_promote_does_not_unblock_on_cancelled_parent(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    """A cancelled parent should NOT allow children to promote."""
    from surogates.tasks.dispatcher import _promote_todo_to_ready

    sp = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        p = Task(org_id=seeded_org_id, parent_session_id=sp, goal="p", status="cancelled")
        c = Task(org_id=seeded_org_id, parent_session_id=sp, goal="c", status="todo")
        db.add_all([p, c])
        await db.flush()
        db.add(TaskLink(parent_id=p.id, child_id=c.id))
        await db.commit()
        cid = c.id

    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        still_todo = await db.get(Task, cid)
        assert still_todo.status == "todo"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_dispatcher.py -v
```

Expected: 3 FAIL with `ImportError: cannot import name '_promote_todo_to_ready'`.

- [ ] **Step 3: Implement `surogates/tasks/dispatcher.py`**

```python
"""Periodic tick that drives task state transitions.

Three steps, each safe to run repeatedly:
  1. Promote tasks whose parents have all reached 'done' from todo → ready.
  2. Finalize tasks whose current Session has ended (mapping completion
     event → done/failed, or scheduling retry).
  3. Enqueue 'ready' tasks: atomically claim, create a child Session,
     update the Task row, and push the Session id to the Redis work queue.

Hosted in the orchestrator dispatcher loop at a 5-second cadence
(see surogates/orchestrator/dispatcher.py integration in Task 10).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text, update

from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.db.models import Session as ORMSession, Task

logger = logging.getLogger(__name__)


_PROMOTE_SQL = text(
    """
    UPDATE tasks SET status = 'ready'
     WHERE status = 'todo'
       AND NOT EXISTS (
         SELECT 1 FROM task_links tl
         JOIN tasks p ON p.id = tl.parent_id
         WHERE tl.child_id = tasks.id
           AND p.status != 'done'
       )
    """
)


async def _promote_todo_to_ready(db) -> int:
    """Bulk-update todo → ready where all parents are 'done'.

    Cancelled / failed parents intentionally do not promote children — those
    children stay in 'todo' until either the orchestrator cancels them or
    a human intervenes.
    """
    result = await db.execute(_PROMOTE_SQL)
    return result.rowcount or 0


_MAX_ENQUEUES_PER_TICK = 10


async def _enqueue_ready_tasks(
    db,
    *,
    redis,
    session_store,
    session_factory,
    tenant_for_task,
) -> int:
    """Atomically claim up to _MAX_ENQUEUES_PER_TICK ready tasks.

    For each: create a child Session via _create_session_for_task, update
    the Task row to 'running', then enqueue the child session.

    tenant_for_task is a callable (Task) -> TenantContext, since the tick
    runs outside any per-request context. In the current worker process,
    construct it from the configured org row and the task's parent session
    using the same tenant-building logic as `harness_factory`.
    """
    from surogates.tasks.spawn import _create_session_for_task

    enqueued = 0
    for _ in range(_MAX_ENQUEUES_PER_TICK):
        task = await db.scalar(
            select(Task)
            .where(Task.status == "ready")
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            break
        tenant = tenant_for_task(task)
        child = await _create_session_for_task(
            task,
            session_store=session_store,
            session_factory=session_factory,
            tenant=tenant,
        )
        task.current_session_id = child.id
        task.status = "running"
        task.attempt_count = (task.attempt_count or 0) + 1
        if task.started_at is None:
            task.started_at = func.now()
        await db.commit()
        await enqueue_session(redis, child.agent_id, child.id)
        enqueued += 1
    return enqueued


async def _finalize_ended_sessions(
    db,
    *,
    session_store,
) -> int:
    """For tasks whose current Session has ended, map to done/failed/retry.

    Completion is reported to the parent session as WORKER_COMPLETE with
    task_id in the payload. The child session log itself ends with
    SESSION_COMPLETE, so do not look for WORKER_COMPLETE in the child log.
      - parent WORKER_COMPLETE for this task/current worker → done
      - parent TASK_BLOCKED for this task/current worker    → blocked
      - otherwise                                          → retry/fail
    """
    from surogates.session.events import EventType

    rows = (await db.execute(
        select(Task, ORMSession)
        .join(ORMSession, ORMSession.id == Task.current_session_id)
        .where(Task.status == "running")
        .where(ORMSession.status.in_(("ended", "failed", "completed")))
    )).all()

    finalized = 0
    for task, sess in rows:
        parent_events = await session_store.get_events(task.parent_session_id)
        matching_event = None
        for event in reversed(parent_events):
            if event.type not in {
                EventType.WORKER_COMPLETE.value,
                EventType.TASK_BLOCKED.value,
            }:
                continue
            data = event.data or {}
            if (
                data.get("task_id") == str(task.id)
                and data.get("worker_id") == str(sess.id)
            ):
                matching_event = event
                break

        if matching_event is not None and matching_event.type == EventType.WORKER_COMPLETE.value:
            task.status = "done"
            task.result = (matching_event.data or {}).get("result")
            task.completed_at = func.now()
            finalized += 1
        elif matching_event is not None and matching_event.type == EventType.TASK_BLOCKED.value:
            if task.status == "running":
                task.status = "blocked"
            # Otherwise already handled by the tool; skip.
        else:
            # No completion / block event — treat as crash/timeout.
            if task.attempt_count >= task.max_attempts:
                task.status = "failed"
                task.completed_at = func.now()
                await session_store.emit_event(
                    task.parent_session_id, EventType.TASK_FAILED,
                    {
                        "task_id": str(task.id),
                        "worker_id": str(sess.id),
                        "attempt_count": task.attempt_count,
                    },
                )
            else:
                task.status = "ready"  # retry
            finalized += 1
    await db.commit()
    return finalized


async def tasks_tick(
    *,
    session_factory,
    redis,
    session_store,
    tenant_for_task,
) -> dict[str, int]:
    """Run one tick of the task layer. Returns counts for observability."""
    async with session_factory() as db:
        promoted = await _promote_todo_to_ready(db)
        await db.commit()
    async with session_factory() as db:
        finalized = await _finalize_ended_sessions(db, session_store=session_store)
    async with session_factory() as db:
        enqueued = await _enqueue_ready_tasks(
            db, redis=redis, session_store=session_store,
            session_factory=session_factory, tenant_for_task=tenant_for_task,
        )
    return {"promoted": promoted, "finalized": finalized, "enqueued": enqueued}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_dispatcher.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/tasks/dispatcher.py tests/tasks/test_dispatcher.py
git commit -m "feat(tasks): tasks_tick with promote/finalize/enqueue"
```

---

## Task 10: Wire `tasks_tick` into the orchestrator + integration test

**Files:**
- Modify: `surogates/orchestrator/dispatcher.py` (add 5s timer loop calling `tasks_tick`)
- Test: `tests/tasks/test_integration.py` (new) — end-to-end via real DB + mocked Redis

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_integration.py`:

```python
"""End-to-end integration tests for the subagent task layer.

Exercise the full spawn_task → tick → WORKER_COMPLETE → child promotion flow
with a real (test) database and a stubbed Redis.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from surogates.db.models import Org, Session as ORMSession, Task, TaskLink
from surogates.session.events import EventType


@pytest.mark.asyncio
async def test_fan_in_synthesizer_promotes_when_parents_done(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    """Two researcher tasks complete → synthesizer auto-promotes from todo to ready."""
    from surogates.tasks.dispatcher import _promote_todo_to_ready

    sp = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        r1 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="research cost", status="running")
        r2 = Task(org_id=seeded_org_id, parent_session_id=sp, goal="research latency", status="running")
        synth = Task(org_id=seeded_org_id, parent_session_id=sp, goal="synthesize", status="todo")
        db.add_all([r1, r2, synth])
        await db.flush()
        db.add_all([
            TaskLink(parent_id=r1.id, child_id=synth.id),
            TaskLink(parent_id=r2.id, child_id=synth.id),
        ])
        await db.commit()
        synth_id = synth.id
        r1_id = r1.id
        r2_id = r2.id

    # Initially: synth is todo (one parent done is not enough)
    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        s = await db.get(Task, synth_id)
        assert s.status == "todo"

    # Mark r1 done, run tick → still todo
    async with async_session_factory() as db:
        r1 = await db.get(Task, r1_id)
        r1.status = "done"
        await db.commit()
    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        s = await db.get(Task, synth_id)
        assert s.status == "todo"

    # Mark r2 done, run tick → promoted to ready
    async with async_session_factory() as db:
        r2 = await db.get(Task, r2_id)
        r2.status = "done"
        await db.commit()
    async with async_session_factory() as db:
        await _promote_todo_to_ready(db)
        await db.commit()
        s = await db.get(Task, synth_id)
        assert s.status == "ready"


@pytest.mark.asyncio
async def test_worker_complete_parent_event_marks_task_done(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    """Parent WORKER_COMPLETE with matching task_id finalizes the task as done."""
    from surogates.tasks.dispatcher import _finalize_ended_sessions

    sp = uuid.uuid4()
    ws = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        db.add(ORMSession(id=ws, org_id=seeded_org_id, agent_id="a", channel="task", status="completed"))
        t = Task(
            org_id=seeded_org_id, parent_session_id=sp, goal="g",
            status="running", current_session_id=ws, attempt_count=1, max_attempts=3,
        )
        db.add(t)
        await db.commit()
        tid = t.id

    event = MagicMock(
        type=EventType.WORKER_COMPLETE.value,
        data={"task_id": str(tid), "worker_id": str(ws), "result": "done!"},
    )
    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[event])

    async with async_session_factory() as db:
        await _finalize_ended_sessions(db, session_store=store)
        done = await db.get(Task, tid)
        assert done.status == "done"
        assert done.result == "done!"


@pytest.mark.asyncio
async def test_crash_retries_within_max_attempts(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    """A running task whose session ended without WORKER_COMPLETE retries up to max_attempts."""
    from surogates.tasks.dispatcher import _finalize_ended_sessions

    sp = uuid.uuid4()
    ws = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        db.add(ORMSession(id=ws, org_id=seeded_org_id, agent_id="a", channel="task", status="ended"))
        t = Task(
            org_id=seeded_org_id, parent_session_id=sp, goal="g",
            status="running", current_session_id=ws, attempt_count=1, max_attempts=3,
        )
        db.add(t)
        await db.commit()
        tid = t.id

    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])  # no completion event

    async with async_session_factory() as db:
        await _finalize_ended_sessions(db, session_store=store)
        retried = await db.get(Task, tid)
        assert retried.status == "ready"  # retry, not failed (attempts remain)


@pytest.mark.asyncio
async def test_crash_after_max_attempts_marks_failed(
    async_session_factory, seeded_org_id: uuid.UUID,
):
    """A running task that has used all attempts transitions to failed."""
    from surogates.tasks.dispatcher import _finalize_ended_sessions

    sp = uuid.uuid4()
    ws = uuid.uuid4()
    async with async_session_factory() as db:
        db.add(ORMSession(id=sp, org_id=seeded_org_id, agent_id="a", channel="web", status="active"))
        db.add(ORMSession(id=ws, org_id=seeded_org_id, agent_id="a", channel="task", status="ended"))
        t = Task(
            org_id=seeded_org_id, parent_session_id=sp, goal="g",
            status="running", current_session_id=ws, attempt_count=3, max_attempts=3,
        )
        db.add(t)
        await db.commit()
        tid = t.id

    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.emit_event = AsyncMock(return_value=1)

    async with async_session_factory() as db:
        await _finalize_ended_sessions(db, session_store=store)
        failed = await db.get(Task, tid)
        assert failed.status == "failed"
        # TASK_FAILED emitted to parent
        emit_calls = store.emit_event.call_args_list
        failed_calls = [c for c in emit_calls if c[0][1] == EventType.TASK_FAILED]
        assert len(failed_calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /work/surogates && uv run pytest tests/tasks/test_integration.py -v
```

Expected: 4 PASS after Task 9. These tests exercise the dispatcher functions before wiring the long-running tick loop; if any fail, fix Task 9 before continuing.

- [ ] **Step 3: Wire `tasks_tick` into the orchestrator**

Open `surogates/orchestrator/dispatcher.py`. Near the top alongside the existing interval constants (`_ORPHAN_SWEEP_INTERVAL = 60.0` etc.), add:

```python
# Subagent task layer tick: promote, finalize, enqueue.
_TASKS_TICK_INTERVAL: float = 5.0
```

In `Orchestrator.__init__`, accept the additional kwargs needed by `tasks_tick`:

```python
def __init__(
    self,
    redis_client: Redis,
    session_store: SessionStore,
    harness_factory: Callable[..., Any],
    *,
    agent_id: str,
    queue_key: str,
    max_concurrent: int = 50,
    poll_timeout: int = 5,
    browser_pool: BrowserPool | None = None,
    session_factory: Any | None = None,       # NEW
    tenant_for_task: Any | None = None,        # NEW: (Task) -> TenantContext
) -> None:
    # ... existing assignments ...
    self._session_factory = session_factory
    self._tenant_for_task = tenant_for_task
```

In the orchestrator's main loop / startup logic (look for where the orphan sweep is launched — `asyncio.create_task(self._orphan_sweep_loop())` or similar), add a parallel task for the tasks tick:

```python
async def _tasks_tick_loop(self) -> None:
    """Periodic tick that drives task layer state transitions."""
    if self._session_factory is None or self._tenant_for_task is None:
        logger.warning("tasks_tick disabled: session_factory or tenant_for_task missing")
        return
    from surogates.tasks.dispatcher import tasks_tick
    while self._running:
        try:
            await tasks_tick(
                session_factory=self._session_factory,
                redis=self.redis,
                session_store=self.session_store,
                tenant_for_task=self._tenant_for_task,
            )
        except Exception:
            logger.exception("tasks_tick failed")
        await asyncio.sleep(_TASKS_TICK_INTERVAL)
```

And launch it alongside the orphan sweep at orchestrator startup:

```python
# In run(), alongside the existing orphan sweep launch:
tasks_tick_task = asyncio.create_task(
    self._tasks_tick_loop(),
    name="tasks-tick",
)
```

Cancel it alongside `interrupt_task` and `orphan_sweeper_task` during shutdown:

```python
for task in (interrupt_task, orphan_sweeper_task, tasks_tick_task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

Do not add this long-running loop to `self._tasks`; that set tracks per-session harness tasks and is drained separately on shutdown.

- [ ] **Step 4: Run all task-layer tests + existing orchestrator tests**

```bash
cd /work/surogates && uv run pytest tests/tasks/ tests/test_coordinator.py -v
```

Expected: all pass. Then run the broader suite to catch regressions:

```bash
cd /work/surogates && uv run pytest tests/ -x --ignore=tests/integration 2>&1 | tail -20
```

Expected: no new failures vs main.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates && git add surogates/orchestrator/dispatcher.py tests/tasks/test_integration.py
git commit -m "feat(tasks): wire tasks_tick into orchestrator at 5s cadence"
```

---

## Post-implementation

After all 10 tasks land:

1. **Existing prod data**: `sessions.task_id` is added as `nullable=True` in the ORM, and `surogates/db/observability.sql` must contain guarded retrofit DDL for `tasks`, `task_links`, `sessions.task_id`, and their indexes. This repo's migration path is currently `Base.metadata.create_all` plus `apply_observability_ddl`; do not rely on `create_all` alone for existing deployments because it skips existing tables and will not add the new column.

2. **AgentDef discovery in coordinator prompt**: `prompt.py` already lists "# Available Sub-Agents" for coordinator sessions. Verify the coordinator's prompt now mentions `spawn_task` alongside `spawn_worker` so the LLM knows when to reach for each — this is a docs/prompt change, not code, but worth doing in the same release.

3. **Operational visibility**: `tasks_tick` returns `{promoted, finalized, enqueued}`. Wire those into the orchestrator's existing metrics emission (search for `prometheus` or `metrics.` in dispatcher.py and add the same alongside).

4. **Update sub-agent docs**: add a section to `/work/surogates/docs/sub-agents/index.md` describing when to choose `spawn_task` vs `spawn_worker`.

---

## Self-Review Checklist (run after writing this plan)

- ✅ **Spec coverage**: every §4-§7 spec section maps to a task above.
- ✅ **No placeholders**: every step has real code or a real command, no TBDs.
- ✅ **Type consistency**: `task_id` used the same way across Pydantic Session, ORM Session, WORKER_COMPLETE payload, and `_create_session_for_task`. `agent_def_name` used everywhere (not `agent_type` on the row — that's the tool kwarg). `attempt_count` always int, defaulted to 0.
- ⚠️ **Open integration verifications** (deliberately flagged in the spec §7): where `coordinator.register()` is called from (verified by grep at Task 7), and the exact tenant construction path for dispatcher-owned retries (wired in Task 10). `SessionStore.create_session` / `create_child_session` do not accept `task_id` today; Task 3 explicitly extends them.
