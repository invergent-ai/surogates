# `/mission` Orchestrated Goals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `/mission` command, mission evaluator, REST API, dedicated mission dashboard, worker-activity visibility, and orchestrator skill guidance per the v1 design.

**Architecture:** New `surogates/missions/` package holds the Pydantic model, store, slash command handlers, and evaluator. The mission lives on the current chat session (the coordinator). The evaluator fires only after the coordinator's next no-tool-call response when there is a mission trigger: a mission-linked task reached a terminal state, or the coordinator emitted `[[mission-complete]]` on its own line. Workers spawned via `spawn_task` inherit `mission_id`, and the dashboard reads mission state through REST endpoints under `surogates/api/routes/missions.py`.

**Tech Stack:** Python 3.12, async SQLAlchemy 2.x (Postgres), pytest + pytest-asyncio with testcontainers, Redis (already plumbed via the task layer), FastAPI for REST, React/Vite/TanStack Router for the dashboard.

**Reference spec:** [`docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md`](../specs/2026-05-16-mission-orchestrated-goals-design.md)

---

## Implementation Progress

Update before each commit. Legend: `[ ]` not started · `[~]` in progress · `[x]` complete.

- [x] **Task 1**: SQLAlchemy schema — `missions` table, `tasks.mission_id`, retrofit DDL, Pydantic Mission, EventType extensions
- [x] **Task 2**: Mission store (CRUD + rate-limit guard + mutual-exclusion check)
- [x] **Task 3**: `/mission` command parser (description + rubric + subcommand dispatch)
- [ ] **Task 4**: `/mission` create handler with `/goal` mutual exclusion + kickoff event + skill preload
- [ ] **Task 5**: `/mission status` + pause/resume/cancel slash handlers
- [ ] **Task 6**: Cascade cancel (issues `cancel_task` per non-terminal child)
- [ ] **Task 7**: `spawn_task` stamps `mission_id` from active mission
- [ ] **Task 8**: Mission evaluator — trigger detection + rate-limit guard
- [ ] **Task 9**: Mission evaluator — prompt building + verdict handling + continuation message
- [ ] **Task 10**: Wire evaluator into the harness loop
- [ ] **Task 11**: REST API — GET endpoints (list, detail, tasks, workers)
- [ ] **Task 12**: REST API — POST endpoints (pause, resume, cancel)
- [ ] **Task 13**: Frontend mission API client + route registration
- [ ] **Task 14**: Dedicated mission dashboard page with task graph, worker activity, evidence, and controls
- [ ] **Task 15**: Skill addendum — `subagent-task-orchestrator` criterion-loop section

**Test placement:** DB-backed tests under `tests/integration/missions/` to inherit testcontainers fixtures from `tests/integration/conftest.py`. Pure mock-based tests under `tests/missions/`.
Frontend tests live under `web/src/**/__tests__/` or the existing frontend test pattern if one is present when implementation starts.

---

## File Map

**New files:**
- `surogates/missions/__init__.py` — package init
- `surogates/missions/models.py` — Pydantic `Mission` domain model + status enum
- `surogates/missions/store.py` — async DB CRUD layer
- `surogates/missions/commands.py` — `/mission` parser + slash handlers
- `surogates/missions/evaluator.py` — trigger detection, prompt building, verdict handling
- `surogates/api/routes/missions.py` — FastAPI routes
- `tests/missions/__init__.py`
- `tests/missions/conftest.py` — `_make_mission` etc. mock helpers
- `tests/missions/test_models.py`
- `tests/missions/test_commands_parser.py`
- `tests/missions/test_evaluator_unit.py`
- `tests/integration/missions/__init__.py`
- `tests/integration/missions/conftest.py` — `org_id`, `parent_session`, `make_active_mission` fixtures
- `tests/integration/missions/test_schema.py`
- `tests/integration/missions/test_store.py`
- `tests/integration/missions/test_commands.py`
- `tests/integration/missions/test_spawn_task_mission_id.py`
- `tests/integration/missions/test_evaluator.py`
- `tests/integration/missions/test_api.py`
- `web/src/api/missions.ts` — typed mission REST client
- `web/src/types/mission.ts` — dashboard-facing mission/task/worker types
- `web/src/app/routes/missions.tsx` — route parent for `/missions`
- `web/src/app/routes/mission-detail.tsx` — route for `/missions/$missionId`
- `web/src/features/missions/mission-page.tsx` — dedicated mission dashboard

**Modified files:**
- `surogates/db/models.py` — add SQLAlchemy `Mission`; add `mission_id` column on `Task`
- `surogates/db/observability.sql` — retrofit DDL for `missions` + `tasks.mission_id`
- `surogates/session/events.py` — `MISSION_DEFINED`, `MISSION_EVALUATION_START`, `MISSION_EVALUATION_END`, `MISSION_CONTINUATION`, `MISSION_PAUSED`, `MISSION_RESUMED`, `MISSION_CANCELLED`
- `surogates/tasks/tools.py` — `_spawn_task_handler` reads `session.config["active_mission_id"]` and stamps the new Task
- `surogates/harness/loop.py` — register `/mission` slash dispatch alongside `/goal`; call mission evaluator after no-tool-call responses
- `surogates/harness/slash_skill.py` — reserve `mission` so `/mission` is never expanded as a dynamic skill
- `surogates/api/app.py` — include the missions router
- `web/src/app/router.tsx` — register mission routes
- `skills/kanban/subagent-task-orchestrator/SKILL.md` — criterion-loop section

---

## Task 1: SQLAlchemy schema + Pydantic model + event types

**Files:**
- Create: `surogates/missions/__init__.py` (empty)
- Create: `surogates/missions/models.py`
- Modify: `surogates/db/models.py` (append `Mission`; add `mission_id` to `Task`)
- Modify: `surogates/db/observability.sql` (retrofit DDL block)
- Modify: `surogates/session/events.py` (7 new `EventType` members)
- Create: `tests/integration/missions/__init__.py` (empty)
- Create: `tests/integration/missions/test_schema.py`
- Create: `tests/missions/__init__.py` (empty)
- Create: `tests/missions/test_models.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/missions/test_schema.py`:

```python
"""Schema tests for the Mission ORM model + tasks.mission_id column."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from surogates.db.models import (
    Mission,
    Session as ORMSession,
    Task,
)

from tests.integration.conftest import create_org, create_user


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(session_factory, org_id) -> uuid.UUID:
    return await create_user(session_factory, org_id)


@pytest_asyncio.fixture(loop_scope="session")
async def chat_session(session_factory, org_id, user_id):
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{sid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


@pytest.mark.asyncio(loop_scope="session")
async def test_mission_round_trip_with_defaults(session_factory, org_id, user_id, chat_session):
    """Mission row persists with the documented defaults."""
    async with session_factory() as db:
        db.add(Mission(
            id=uuid.uuid4(),
            org_id=org_id,
            user_id=user_id,
            session_id=chat_session.id,
            agent_id="orchestrator",
            description="Train a 0.6B model and hit 0.8 on gsm8k",
            rubric="A verifier task must report result_metadata.score >= 0.8",
        ))
        await db.commit()

    async with session_factory() as db:
        m = (await db.execute(select(Mission).where(Mission.session_id == chat_session.id))).scalar_one()
        assert m.status == "active"
        assert m.iteration == 0
        assert m.max_iterations == 20
        assert m.evaluator_parse_failures == 0
        assert m.last_evaluation_result is None
        assert m.last_evaluation_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_tasks_mission_id_fk(session_factory, org_id, user_id, chat_session):
    """tasks.mission_id is nullable and FKs to missions(id)."""
    async with session_factory() as db:
        mission = Mission(
            id=uuid.uuid4(), org_id=org_id, user_id=user_id,
            session_id=chat_session.id, agent_id="orchestrator",
            description="g", rubric="r",
        )
        db.add(mission)
        await db.flush()
        task = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="research", status="ready", mission_id=mission.id,
        )
        db.add(task)
        await db.commit()
        tid = task.id
        mid = mission.id

    async with session_factory() as db:
        loaded = await db.get(Task, tid)
        assert loaded.mission_id == mid


@pytest.mark.asyncio(loop_scope="session")
async def test_tasks_mission_id_defaults_null(session_factory, org_id, chat_session):
    """A non-mission Task has mission_id == None."""
    async with session_factory() as db:
        t = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="solo", status="ready",
        )
        db.add(t)
        await db.commit()
        tid = t.id
    async with session_factory() as db:
        loaded = await db.get(Task, tid)
        assert loaded.mission_id is None
```

Create `tests/missions/test_models.py`:

```python
"""Unit tests for Pydantic Mission + EventType extensions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


def test_pydantic_mission_constructible_from_orm_attributes():
    """Pydantic Mission constructs from a duck-typed row via from_attributes."""
    from surogates.missions.models import Mission as PydMission

    fake = type("FakeRow", (), {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "agent_id": "orchestrator",
        "description": "train model",
        "rubric": "gsm8k >= 0.8",
        "status": "active",
        "iteration": 0,
        "max_iterations": 20,
        "last_evaluation_result": None,
        "last_evaluation_explanation": None,
        "last_evaluation_feedback": None,
        "last_evaluation_at": None,
        "evaluator_parse_failures": 0,
        "paused_reason": None,
        "cancelled_reason": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })()
    pyd = PydMission.model_validate(fake)
    assert pyd.status == "active"
    assert pyd.iteration == 0


def test_pydantic_mission_rejects_unknown_status():
    """Status is constrained to the documented state machine."""
    from pydantic import ValidationError

    from surogates.missions.models import Mission as PydMission

    with pytest.raises(ValidationError):
        PydMission(
            id=uuid.uuid4(), org_id=uuid.uuid4(), user_id=uuid.uuid4(),
            session_id=uuid.uuid4(), agent_id="a",
            description="g", rubric="r",
            status="bogus", iteration=0, max_iterations=20,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )


def test_event_types_include_mission_events():
    """EventType enum exposes the 7 mission lifecycle events."""
    from surogates.session.events import EventType

    assert EventType.MISSION_DEFINED.value == "mission.defined"
    assert EventType.MISSION_EVALUATION_START.value == "mission.evaluation.start"
    assert EventType.MISSION_EVALUATION_END.value == "mission.evaluation.end"
    assert EventType.MISSION_CONTINUATION.value == "mission.continuation"
    assert EventType.MISSION_PAUSED.value == "mission.paused"
    assert EventType.MISSION_RESUMED.value == "mission.resumed"
    assert EventType.MISSION_CANCELLED.value == "mission.cancelled"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /work/surogates && uv run pytest tests/missions/test_models.py tests/integration/missions/test_schema.py -v
```

Expected: ImportError on `Mission`, `MISSION_*` enum members, `surogates.missions.models`.

- [ ] **Step 3: Add 7 EventType members**

In `surogates/session/events.py`, after the `TASK_BLOCKED` / `TASK_FAILED` entries:

```python
    # Subagent task layer (existing)
    TASK_BLOCKED = "task.blocked"
    TASK_FAILED = "task.failed"

    # Mission layer (orchestrated goals).
    # Emitted on the coordinator chat session. The dashboard polls these
    # to render mission state; see docs/superpowers/specs/2026-05-16-mission-
    # orchestrated-goals-design.md.
    MISSION_DEFINED = "mission.defined"
    MISSION_EVALUATION_START = "mission.evaluation.start"
    MISSION_EVALUATION_END = "mission.evaluation.end"
    MISSION_CONTINUATION = "mission.continuation"
    MISSION_PAUSED = "mission.paused"
    MISSION_RESUMED = "mission.resumed"
    MISSION_CANCELLED = "mission.cancelled"
```

- [ ] **Step 4: Add SQLAlchemy `Mission` model and `tasks.mission_id` column**

In `surogates/db/models.py`, locate the `Task` class. After its `max_attempts` column, before `created_at`, add:

```python
    # Mission layer: when set, this task belongs to a mission (the
    # coordinator session is the mission's session). Stamped at spawn
    # time by `_spawn_task_handler` reading `session.config["active_mission_id"]`.
    # Nullable so non-mission tasks (plain spawn_task, or spawn_task from
    # a session without an active mission) carry None.
    mission_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id"), nullable=True
    )
```

After the last existing model in the file, append:

```python
# ---------------------------------------------------------------------------
# Mission layer
#
# A Mission is a long-running, durable, multi-worker objective attached
# to a chat (coordinator) session. The mission's rubric is graded by an
# LLM judge fired on (a) mission-task terminal events, or (b) an
# explicit ``[[mission-complete]]`` marker in the coordinator's prose —
# never on every no-tool-call response (that's `/goal`'s rule and it's
# wrong for orchestrator workloads).
#
# See docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md.
# ---------------------------------------------------------------------------


class Mission(Base):
    """A durable orchestrated objective with rubric-judged completion."""

    __tablename__ = "missions"
    __table_args__ = (
        Index("idx_missions_session", "session_id"),
        Index("idx_missions_user_agent_status", "org_id", "user_id", "agent_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rubric: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active",
    )
    iteration: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="20"
    )
    last_evaluation_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_evaluation_explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_evaluation_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_evaluation_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    evaluator_parse_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    paused_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 5: Add retrofit DDL to observability.sql**

In `surogates/db/observability.sql`, after the existing subagent-task-layer retrofit block, add:

```sql
-- ----------------------------------------------------------------------------
-- Mission layer (orchestrated goals) — retrofits.
-- ``Base.metadata.create_all`` creates ``missions`` and the indexes on
-- fresh databases, but does NOT add ``tasks.mission_id`` to an existing
-- ``tasks`` table. Each statement guarded for idempotent re-runs.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS missions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                      uuid NOT NULL REFERENCES orgs(id),
    user_id                     uuid NOT NULL REFERENCES users(id),
    session_id                  uuid NOT NULL REFERENCES sessions(id),
    agent_id                    text NOT NULL,
    description                 text NOT NULL,
    rubric                      text NOT NULL,
    status                      text NOT NULL DEFAULT 'active',
    iteration                   integer NOT NULL DEFAULT 0,
    max_iterations              integer NOT NULL DEFAULT 20,
    last_evaluation_result      text,
    last_evaluation_explanation text,
    last_evaluation_feedback    text,
    last_evaluation_at          timestamptz,
    evaluator_parse_failures    integer NOT NULL DEFAULT 0,
    paused_reason               text,
    cancelled_reason            text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS mission_id uuid REFERENCES missions(id);

CREATE INDEX IF NOT EXISTS idx_missions_session
    ON missions (session_id);
CREATE INDEX IF NOT EXISTS idx_missions_user_agent_status
    ON missions (org_id, user_id, agent_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_mission
    ON tasks (mission_id);
```

- [ ] **Step 6: Create the Pydantic Mission model**

Create `surogates/missions/__init__.py` (empty file).

Create `surogates/missions/models.py`:

```python
"""Pydantic domain model for Mission — mirrors the ORM row.

Used throughout the application layer. Constructible from a
``surogates.db.models.Mission`` row via ``model_config = {"from_attributes": True}``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


MissionStatus = Literal[
    "active",
    "paused",
    "satisfied",
    "blocked",
    "failed",
    "cancelled",
    "max_iterations_reached",
]
"""Type alias mirroring the spec's status state machine.

* ``active``                — created, evaluator firing on triggers
* ``paused``                — evaluator suspended; workers continue
* ``satisfied``             — terminal; rubric judge returned satisfied
* ``blocked``               — terminal; coordinator or judge marked blocked
* ``failed``                — terminal; coordinator or judge marked failed
* ``cancelled``             — terminal; user cancelled (workers may still run
                              unless cascade_to_workers=True was passed)
* ``max_iterations_reached`` — terminal; bumped past max_iterations on
                              repeated needs_revision verdicts
"""


EvaluationResult = Literal[
    "satisfied",
    "needs_revision",
    "blocked",
    "failed",
]


class Mission(BaseModel):
    """Snapshot of a Mission row."""

    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    user_id: UUID
    session_id: UUID
    agent_id: str
    description: str
    rubric: str
    status: MissionStatus
    iteration: int = 0
    max_iterations: int = 20
    last_evaluation_result: EvaluationResult | None = None
    last_evaluation_explanation: str | None = None
    last_evaluation_feedback: str | None = None
    last_evaluation_at: datetime | None = None
    evaluator_parse_failures: int = 0
    paused_reason: str | None = None
    cancelled_reason: str | None = None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd /work/surogates && uv run pytest tests/missions/test_models.py tests/integration/missions/test_schema.py -v
```

Expected: 6 PASS (3 unit + 3 integration).

- [ ] **Step 8: Commit**

```bash
git add surogates/missions/__init__.py surogates/missions/models.py surogates/db/models.py surogates/db/observability.sql surogates/session/events.py tests/missions/ tests/integration/missions/__init__.py tests/integration/missions/test_schema.py
git commit -m "feat(missions): Mission ORM + Pydantic + tasks.mission_id + event types"
```

---

## Task 2: Mission store (CRUD + rate-limit guard + mutual-exclusion check)

**Files:**
- Create: `surogates/missions/store.py`
- Create: `tests/integration/missions/conftest.py` (shared fixtures)
- Create: `tests/integration/missions/test_store.py`

- [ ] **Step 1: Write the failing test + shared conftest**

Create `tests/integration/missions/conftest.py`:

```python
"""Shared fixtures for mission integration tests."""
from __future__ import annotations

import uuid

import pytest_asyncio

from surogates.db.models import (
    Mission,
    Session as ORMSession,
    User,
)

from tests.integration.conftest import create_org, create_user


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory) -> uuid.UUID:
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(session_factory, org_id) -> uuid.UUID:
    return await create_user(session_factory, org_id)


@pytest_asyncio.fixture(loop_scope="session")
async def chat_session(session_factory, org_id, user_id):
    sid = uuid.uuid4()
    async with session_factory() as db:
        s = ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{sid}",
                "supports_vision": False,
            },
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s
```

Create `tests/integration/missions/test_store.py`:

```python
"""Tests for MissionStore CRUD and constraints."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from surogates.db.models import Mission
from surogates.missions.store import (
    MissionStore,
    ActiveMissionConflictError,
    MissionNotFoundError,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_create_and_get(session_factory, org_id, user_id, chat_session):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="train a model", rubric="gsm8k >= 0.8",
    )
    m = await store.get(mid)
    assert m.status == "active"
    assert m.description == "train a model"
    assert m.iteration == 0
    assert m.max_iterations == 20


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_second_active_on_same_session(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="first", rubric="r",
    )
    with pytest.raises(ActiveMissionConflictError):
        await store.create(
            org_id=org_id, user_id=user_id, session_id=chat_session.id,
            agent_id="orchestrator",
            description="second", rubric="r",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_create_allowed_after_previous_terminal(
    session_factory, org_id, user_id, chat_session,
):
    """Once the previous mission is terminal, a new one is allowed."""
    store = MissionStore(session_factory)
    first = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="first", rubric="r",
    )
    await store.set_status(first, "satisfied")
    # Second mission should now succeed.
    second = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="second", rubric="r2",
    )
    assert second != first


@pytest.mark.asyncio(loop_scope="session")
async def test_get_active_for_session_returns_active_or_paused(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    got = await store.get_active_for_session(chat_session.id)
    assert got.id == mid
    await store.set_status(mid, "paused", paused_reason="manual")
    got2 = await store.get_active_for_session(chat_session.id)
    assert got2.id == mid


@pytest.mark.asyncio(loop_scope="session")
async def test_get_active_for_session_none_after_terminal(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    await store.set_status(mid, "cancelled", cancelled_reason="user")
    assert await store.get_active_for_session(chat_session.id) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_record_evaluation_writes_fields(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    await store.record_evaluation(
        mid, result="needs_revision",
        explanation="not yet", feedback="try more data",
    )
    m = await store.get(mid)
    assert m.last_evaluation_result == "needs_revision"
    assert m.last_evaluation_explanation == "not yet"
    assert m.last_evaluation_feedback == "try more data"
    assert m.last_evaluation_at is not None
    assert m.evaluator_parse_failures == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_record_parse_failure_pauses_after_three(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    assert await store.record_parse_failure(mid) == 1
    assert await store.record_parse_failure(mid) == 2
    assert await store.record_parse_failure(mid) == 3
    m = await store.get(mid)
    assert m.status == "paused"
    assert m.paused_reason == "evaluator parse failure"


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_recently_evaluated(
    session_factory, org_id, user_id, chat_session,
):
    """recently_evaluated returns True for <30s since last_evaluation_at."""
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    # Fresh mission: never evaluated → not rate-limited.
    assert not await store.recently_evaluated(mid, window_seconds=30)
    await store.record_evaluation(mid, result="needs_revision", explanation="", feedback="")
    # Immediate re-check → rate-limited.
    assert await store.recently_evaluated(mid, window_seconds=30)


@pytest.mark.asyncio(loop_scope="session")
async def test_increment_iteration(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="orchestrator",
        description="d", rubric="r",
    )
    new_iter = await store.increment_iteration(mid)
    assert new_iter == 1
    again = await store.increment_iteration(mid)
    assert again == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_get_raises_for_unknown_id(session_factory):
    store = MissionStore(session_factory)
    with pytest.raises(MissionNotFoundError):
        await store.get(uuid.uuid4())
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_store.py -v
```

Expected: ImportError on `MissionStore`, `ActiveMissionConflictError`, `MissionNotFoundError`.

- [ ] **Step 3: Implement the store**

Create `surogates/missions/store.py`:

```python
"""DB CRUD layer for missions.

Provides a small async interface used by slash command handlers,
evaluator, and REST routes. Wraps the existing async_sessionmaker
pattern used elsewhere in Surogates (see ``surogates.session.store``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import Mission as MissionRow
from surogates.missions.models import Mission, MissionStatus


_TERMINAL_STATUSES: tuple[str, ...] = (
    "satisfied", "blocked", "failed", "cancelled", "max_iterations_reached",
)
_ACTIVE_OR_PAUSED: tuple[str, ...] = ("active", "paused")


class MissionStoreError(Exception):
    """Base for mission store errors."""


class MissionNotFoundError(MissionStoreError):
    """Raised when a mission id is not in the DB."""


class ActiveMissionConflictError(MissionStoreError):
    """Raised when create() would violate the one-active-per-session rule."""


class MissionStore:
    """Async CRUD for the ``missions`` table.

    All methods take an open ``async_sessionmaker``; transactions are
    short-lived per call.
    """

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        session_id: UUID,
        agent_id: str,
        description: str,
        rubric: str,
        max_iterations: int = 20,
    ) -> UUID:
        """Insert a new mission with status='active'.

        Rejects with :class:`ActiveMissionConflictError` if any mission
        with ``session_id`` is already in ``active`` or ``paused``.
        """
        async with self._sf() as db:
            existing = await db.scalar(
                select(MissionRow.id)
                .where(
                    MissionRow.session_id == session_id,
                    MissionRow.status.in_(_ACTIVE_OR_PAUSED),
                )
                .limit(1)
            )
            if existing is not None:
                raise ActiveMissionConflictError(
                    f"session {session_id} already has an active or paused mission"
                )
            row = MissionRow(
                org_id=org_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                description=description,
                rubric=rubric,
                max_iterations=max_iterations,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id

    async def get(self, mission_id: UUID) -> Mission:
        async with self._sf() as db:
            row = await db.get(MissionRow, mission_id)
            if row is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            return Mission.model_validate(row)

    async def get_active_for_session(self, session_id: UUID) -> Mission | None:
        """Return the session's active or paused mission, if any."""
        async with self._sf() as db:
            row = await db.scalar(
                select(MissionRow)
                .where(
                    MissionRow.session_id == session_id,
                    MissionRow.status.in_(_ACTIVE_OR_PAUSED),
                )
                .limit(1)
            )
        if row is None:
            return None
        return Mission.model_validate(row)

    async def set_status(
        self,
        mission_id: UUID,
        status: MissionStatus,
        *,
        paused_reason: str | None = None,
        cancelled_reason: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if paused_reason is not None:
            values["paused_reason"] = paused_reason
        if cancelled_reason is not None:
            values["cancelled_reason"] = cancelled_reason
        async with self._sf() as db:
            result = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(**values)
            )
            if result.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def record_evaluation(
        self,
        mission_id: UUID,
        *,
        result: str,
        explanation: str,
        feedback: str,
    ) -> None:
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    last_evaluation_result=result,
                    last_evaluation_explanation=explanation,
                    last_evaluation_feedback=feedback,
                    last_evaluation_at=func.now(),
                    evaluator_parse_failures=0,
                )
            )
            if res.rowcount == 0:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()

    async def increment_iteration(self, mission_id: UUID) -> int:
        """Bump iteration by 1; return the new value."""
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(iteration=MissionRow.iteration + 1)
                .returning(MissionRow.iteration)
            )
            new_iter = res.scalar_one_or_none()
            if new_iter is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()
            return int(new_iter)

    async def record_parse_failure(self, mission_id: UUID) -> int:
        """Increment parse failures and pause the mission after 3 consecutive failures."""
        async with self._sf() as db:
            res = await db.execute(
                update(MissionRow)
                .where(MissionRow.id == mission_id)
                .values(
                    evaluator_parse_failures=MissionRow.evaluator_parse_failures + 1,
                    paused_reason=case(
                        (MissionRow.evaluator_parse_failures + 1 >= 3, "evaluator parse failure"),
                        else_=MissionRow.paused_reason,
                    ),
                    status=case(
                        (MissionRow.evaluator_parse_failures + 1 >= 3, "paused"),
                        else_=MissionRow.status,
                    ),
                )
                .returning(MissionRow.evaluator_parse_failures)
            )
            failures = res.scalar_one_or_none()
            if failures is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            await db.commit()
            return int(failures)

    async def recently_evaluated(
        self, mission_id: UUID, *, window_seconds: int,
    ) -> bool:
        """Return True iff ``last_evaluation_at`` is within ``window_seconds``."""
        async with self._sf() as db:
            row = await db.get(MissionRow, mission_id)
            if row is None:
                raise MissionNotFoundError(f"mission {mission_id} not found")
            if row.last_evaluation_at is None:
                return False
            last = row.last_evaluation_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - last < timedelta(seconds=window_seconds)
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_store.py -v
```

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/store.py tests/integration/missions/conftest.py tests/integration/missions/test_store.py
git commit -m "feat(missions): MissionStore with CRUD + rate-limit guard + active-conflict check"
```

---

## Task 3: `/mission` command parser

**Files:**
- Create: `surogates/missions/commands.py` (parser only in this task; handlers in Task 4-5)
- Modify: `surogates/harness/slash_skill.py` (reserve `mission`)
- Create: `tests/missions/test_commands_parser.py`

- [ ] **Step 1: Write the failing test**

Create `tests/missions/test_commands_parser.py`:

```python
"""Unit tests for /mission slash command parsing."""
from __future__ import annotations

import pytest


def test_parse_create_with_rubric():
    """A full create command extracts description + rubric."""
    from surogates.missions.commands import MissionCommand, parse_mission_command

    raw = "Train 0.6B model. Iterate datasets, training, eval.\n\nRubric:\nReach gsm8k score >= 0.8 (verifier task reports result_metadata.score)"
    cmd = parse_mission_command(raw)
    assert cmd.action == "create"
    assert "Train 0.6B model" in cmd.description
    assert "verifier task" in cmd.rubric
    # Description does not include the Rubric: block.
    assert "Rubric:" not in cmd.description


def test_parse_create_rejects_missing_rubric():
    """`/mission <text>` without a Rubric: block fails parse."""
    from surogates.missions.commands import (
        MissionCommandParseError, parse_mission_command,
    )

    with pytest.raises(MissionCommandParseError, match="Rubric"):
        parse_mission_command("just a description")


def test_parse_status():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("status")
    assert cmd.action == "status"


def test_parse_pause_with_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("pause waiting on data review")
    assert cmd.action == "pause"
    assert cmd.reason == "waiting on data review"


def test_parse_pause_without_reason():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("pause")
    assert cmd.action == "pause"
    assert cmd.reason is None


def test_parse_resume():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("resume")
    assert cmd.action == "resume"


def test_parse_cancel_with_reason_and_cascade_flag():
    """`cancel --cascade <reason>` sets cascade_to_workers and captures reason."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("cancel --cascade not viable anymore")
    assert cmd.action == "cancel"
    assert cmd.reason == "not viable anymore"
    assert cmd.cascade_to_workers is True


def test_parse_cancel_without_cascade_default_false():
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("cancel done with this")
    assert cmd.action == "cancel"
    assert cmd.cascade_to_workers is False
    assert cmd.reason == "done with this"


def test_parse_empty_returns_status():
    """`/mission` with no args is a status query (matches /goal)."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("")
    assert cmd.action == "status"


def test_mission_is_reserved_from_slash_skill_expansion():
    """`/mission` must not be treated as a dynamic skill invocation."""
    from surogates.harness.slash_skill import parse_slash_command

    assert parse_slash_command("/mission train model") is None
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/missions/test_commands_parser.py -v
```

Expected: ImportError on `parse_mission_command`.

- [ ] **Step 3: Implement the parser**

Create `surogates/missions/commands.py`:

```python
"""Slash command parsing + handlers for /mission.

The harness loop calls :func:`parse_mission_command` with the args
substring (everything after ``/mission``). Returns a :class:`MissionCommand`
dataclass that handlers in Task 4-5 consume.

Parse-only in this module; the actual DB writes and event emission live
in the handlers (see :func:`handle_mission_command` below — added in
Task 4 + Task 5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


MissionAction = Literal["create", "status", "pause", "resume", "cancel"]


class MissionCommandParseError(ValueError):
    """Raised when /mission args cannot be parsed."""


@dataclass(slots=True)
class MissionCommand:
    """Parsed shape of a /mission invocation."""

    action: MissionAction
    description: str | None = None
    rubric: str | None = None
    reason: str | None = None
    cascade_to_workers: bool = False


_CONTROL_VERBS = ("status", "pause", "resume", "cancel")
_RUBRIC_RE = re.compile(r"\bRubric\s*:", re.IGNORECASE)


def parse_mission_command(raw: str) -> MissionCommand:
    """Parse the args portion of a /mission slash command.

    Empty string → status (matches /goal's behaviour).

    A control verb (``status`` / ``pause`` / ``resume`` / ``cancel``)
    optionally followed by a free-form reason → that action with the
    reason captured. ``cancel --cascade [reason]`` sets
    ``cascade_to_workers=True``.

    Anything else is treated as a ``create`` invocation; it MUST contain
    a ``Rubric:`` block (case-insensitive), otherwise the parse fails.
    """
    text = (raw or "").strip()

    if not text:
        return MissionCommand(action="status")

    # Detect control verb at the start. Match whole-word, then everything
    # after the verb is the optional reason.
    first_token, _, rest = text.partition(" ")
    verb = first_token.lower()
    if verb in _CONTROL_VERBS:
        rest = rest.strip()
        if verb == "cancel":
            cascade = False
            if rest.startswith("--cascade"):
                cascade = True
                rest = rest[len("--cascade"):].strip()
            return MissionCommand(
                action="cancel",
                reason=rest or None,
                cascade_to_workers=cascade,
            )
        if verb == "pause":
            return MissionCommand(action="pause", reason=rest or None)
        if verb == "resume":
            return MissionCommand(action="resume")
        return MissionCommand(action="status")

    # Create flow. Split on the Rubric: marker.
    match = _RUBRIC_RE.search(text)
    if match is None:
        raise MissionCommandParseError(
            "missing Rubric: block. Format: '/mission <description>\\n\\nRubric:\\n<criterion>'"
        )
    description = text[:match.start()].strip()
    rubric = text[match.end():].lstrip(": \n").strip()
    if not description:
        raise MissionCommandParseError("missing description before Rubric: block")
    if not rubric:
        raise MissionCommandParseError("Rubric: block is empty")
    return MissionCommand(
        action="create", description=description, rubric=rubric,
    )
```

Update `surogates/harness/slash_skill.py` so the builtin set includes `mission`:

```python
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({
    "clear",
    "compress",
    "goal",
    "loop",
    "mission",
})
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/missions/test_commands_parser.py -v
```

Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/commands.py surogates/harness/slash_skill.py tests/missions/test_commands_parser.py
git commit -m "feat(missions): /mission slash command parser"
```

---

## Task 4: `/mission` create handler with `/goal` mutual exclusion

**Files:**
- Modify: `surogates/missions/commands.py` (append `handle_mission_create`)
- Modify: `surogates/harness/loop.py` (reject `/goal` creation when the session has an active/paused mission)
- Create: `tests/integration/missions/test_commands.py`

**Correction from review:** mutual exclusion must be bidirectional. This task already rejects `/mission` when a non-terminal `/goal` is active; it must also add the inverse guard so `/goal` cannot start while a mission is `active` or `paused`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/missions/test_commands.py`:

```python
"""Integration tests for /mission slash command handlers."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from surogates.db.models import Event, Session as ORMSession
from surogates.session.events import EventType


@pytest.mark.asyncio(loop_scope="session")
async def test_create_inserts_mission_emits_event_and_kickoff(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A successful /mission create writes a Mission row, emits
    mission.defined, emits a synthetic kickoff user.message, and updates
    session.config with active_mission_id + coordinator=True + the
    preloaded orchestrator skill."""
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    store = MissionStore(session_factory)
    result = await handle_mission_create(
        description="Train 0.6B model",
        rubric="gsm8k >= 0.8 (verifier reports result_metadata.score)",
        session_id=chat_session.id,
        user_id=user_id,
        org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        redis=redis,
    )

    assert result.ok is True
    mid = result.mission_id

    # Mission row exists with active status.
    m = await store.get(mid)
    assert m.status == "active"
    assert m.description == "Train 0.6B model"

    # mission.defined event on the coordinator session.
    async with session_factory() as db:
        defined = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_DEFINED.value,
            )
        )).scalars().all()
        assert len(defined) == 1
        assert defined[0].data["mission_id"] == str(mid)

        # Synthetic kickoff user.message on the session.
        kickoffs = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.USER_MESSAGE.value,
            )
        )).scalars().all()
        assert any(
            ev.data.get("synthetic") == "mission_kickoff"
            for ev in kickoffs
        )

        # Session config updated.
        sess = await db.get(ORMSession, chat_session.id)
        assert sess.config["active_mission_id"] == str(mid)
        assert sess.config["coordinator"] is True
        preloaded = sess.config.get("preloaded_skills") or []
        assert "subagent-task-orchestrator" in preloaded

    # Session enqueued on the agent's work queue.
    redis.zadd.assert_called_once()


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_when_active_goal_present(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """If session.config has a non-terminal /goal outcome, /mission create fails."""
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore
    from surogates.session.store import SessionStore

    # Seed an active outcome on the chat session.
    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        cfg = dict(sess.config or {})
        cfg["outcome"] = {
            "id": "outc_x", "status": "active",
            "description": "...", "rubric": "...",
            "iteration": 0, "max_iterations": 20,
        }
        sess.config = cfg
        await db.commit()

    result = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=MissionStore(session_factory),
        redis=AsyncMock(zadd=AsyncMock()),
    )
    assert result.ok is False
    assert "goal" in result.error.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_when_active_mission_already_on_session(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())

    await handle_mission_create(
        description="first", rubric="r",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    second = await handle_mission_create(
        description="second", rubric="r2",
        session_id=chat_session.id,
        user_id=user_id, org_id=org_id, agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert second.ok is False
    assert "mission" in second.error.lower()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: ImportError on `handle_mission_create`.

- [ ] **Step 3: Implement the create handler**

Append to `surogates/missions/commands.py`:

```python
from dataclasses import dataclass as _dataclass
from typing import Any
from uuid import UUID

from surogates.config import agent_queue_key, enqueue_session
from surogates.db.models import Session as ORMSession
from surogates.missions.store import (
    ActiveMissionConflictError,
    MissionStore,
)
from surogates.session.events import EventType


@_dataclass(slots=True)
class MissionHandlerResult:
    """Standard return shape for slash handlers."""

    ok: bool
    mission_id: UUID | None = None
    message: str = ""
    error: str = ""


_KICKOFF_TEMPLATE = """\
[Mission kickoff]

Description: {description}

Rubric:
{rubric}

You are this mission's coordinator. Decompose into specialist sub-agent
tasks via ``spawn_task``; gate dependencies with ``parents=[...]``;
end criterion-driven rounds with a verifier task whose
``result_metadata`` carries the measurable signal the rubric checks.

Do NOT claim completion in prose alone — the evaluator only honours
completion claims backed by verifier-task evidence OR an explicit
``[[mission-complete]]`` marker on its own line in your response.
"""


def _outcome_is_active(outcome: dict[str, Any] | None) -> bool:
    """True iff session.config['outcome'] represents a non-terminal /goal."""
    if not isinstance(outcome, dict):
        return False
    status = outcome.get("status")
    return status in ("active", "paused")


async def handle_mission_create(
    *,
    description: str,
    rubric: str,
    session_id: UUID,
    user_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    """Create a new mission on the calling session.

    Rejects when the session already has:
    * a non-terminal /goal outcome (``session.config['outcome']`` in
      active/paused), OR
    * an active or paused mission (via :class:`MissionStore`).

    On success: inserts the Mission row, updates session.config with
    ``active_mission_id``, ``coordinator=True``, and the
    ``subagent-task-orchestrator`` preloaded skill; emits
    ``mission.defined``; emits a synthetic ``user.message`` with the
    kickoff prompt; enqueues the session for immediate processing.
    """
    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        if sess is None:
            return MissionHandlerResult(
                ok=False, error=f"session {session_id} not found",
            )
        if _outcome_is_active(sess.config.get("outcome")):
            return MissionHandlerResult(
                ok=False,
                error=(
                    "This session has an active /goal. Clear or pause it "
                    "before starting a /mission (only one evaluator loop "
                    "per session is allowed)."
                ),
            )

    try:
        mission_id = await mission_store.create(
            org_id=org_id, user_id=user_id, session_id=session_id,
            agent_id=agent_id, description=description, rubric=rubric,
        )
    except ActiveMissionConflictError as exc:
        return MissionHandlerResult(ok=False, error=str(exc))

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_mission_id"] = str(mission_id)
        cfg["coordinator"] = True
        preloaded = list(cfg.get("preloaded_skills") or [])
        if "subagent-task-orchestrator" not in preloaded:
            preloaded.append("subagent-task-orchestrator")
        cfg["preloaded_skills"] = preloaded
        sess.config = cfg
        await db.commit()

    await session_store.emit_event(
        session_id, EventType.MISSION_DEFINED,
        {
            "mission_id": str(mission_id),
            "description": description,
            "rubric": rubric,
            "max_iterations": 20,
        },
    )
    kickoff = _KICKOFF_TEMPLATE.format(description=description, rubric=rubric)
    await session_store.emit_event(
        session_id, EventType.USER_MESSAGE,
        {"content": kickoff, "synthetic": "mission_kickoff"},
    )

    await enqueue_session(redis, agent_id, session_id)

    return MissionHandlerResult(
        ok=True, mission_id=mission_id,
        message=f"Mission {mission_id} started.",
    )
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/commands.py tests/integration/missions/test_commands.py
git commit -m "feat(missions): /mission create handler with /goal mutual exclusion + kickoff"
```

---

## Task 5: `/mission status` + pause/resume/cancel slash handlers

**Files:**
- Modify: `surogates/missions/commands.py` (append handlers)
- Modify: `tests/integration/missions/test_commands.py` (append cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_commands.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_status_returns_active_mission_summary(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_status,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    status = await handle_mission_status(
        session_id=chat_session.id, mission_store=store,
    )
    assert status.ok is True
    assert str(created.mission_id) in status.message
    assert "active" in status.message


@pytest.mark.asyncio(loop_scope="session")
async def test_status_when_no_active_mission(
    session_factory, session_store, org_id, user_id,
):
    from surogates.missions.commands import handle_mission_status
    from surogates.missions.store import MissionStore

    fresh = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=fresh, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
        ))
        await db.commit()

    status = await handle_mission_status(
        session_id=fresh, mission_store=MissionStore(session_factory),
    )
    assert status.ok is True
    assert "no active mission" in status.message.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_pause_transitions_status_and_emits_event(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_pause,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    result = await handle_mission_pause(
        session_id=chat_session.id, reason="waiting on review",
        session_store=session_store, mission_store=store,
    )
    assert result.ok is True
    m = await store.get(result.mission_id)
    assert m.status == "paused"
    assert m.paused_reason == "waiting on review"

    async with session_factory() as db:
        evs = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_PAUSED.value,
            )
        )).scalars().all()
        assert len(evs) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_transitions_back_to_active(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_pause, handle_mission_resume,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    await handle_mission_pause(
        session_id=chat_session.id, reason="x",
        session_store=session_store, mission_store=store,
    )
    res = await handle_mission_resume(
        session_id=chat_session.id, agent_id="orchestrator",
        session_store=session_store, mission_store=store, redis=redis,
    )
    assert res.ok is True
    m = await store.get(res.mission_id)
    assert m.status == "active"


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_without_cascade_marks_cancelled(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_cancel,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    res = await handle_mission_cancel(
        session_id=chat_session.id,
        reason="user changed mind",
        cascade_to_workers=False,
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert res.ok is True
    m = await store.get(res.mission_id)
    assert m.status == "cancelled"
    assert m.cancelled_reason == "user changed mind"
    # No cancel_task publish without cascade.
    redis.publish.assert_not_called()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: ImportError on the new handlers.

- [ ] **Step 3: Implement the handlers** (cascade implementation comes in Task 6 — the function exists with `cascade_to_workers` parameter, but it's wired to a stub that always no-ops; Task 6 fills it in)

Append to `surogates/missions/commands.py`:

```python
from surogates.missions.models import Mission


async def handle_mission_status(
    *,
    session_id: UUID,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    """Return a human-readable status string for the session's active mission."""
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(ok=True, message="No active mission on this session.")
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message=(
            f"Mission {active.id}: status={active.status}, "
            f"iteration={active.iteration}/{active.max_iterations}.\n"
            f"Description: {active.description}\n"
            f"Latest evaluator verdict: {active.last_evaluation_result or '(none yet)'}"
        ),
    )


async def handle_mission_pause(
    *,
    session_id: UUID,
    reason: str | None,
    session_store: Any,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(ok=False, error="No active mission to pause.")
    if active.status != "active":
        return MissionHandlerResult(
            ok=False, mission_id=active.id,
            error=f"Mission is not active (status={active.status}); cannot pause.",
        )
    await mission_store.set_status(
        active.id, "paused", paused_reason=reason,
    )
    await session_store.emit_event(
        session_id, EventType.MISSION_PAUSED,
        {"mission_id": str(active.id), "reason": reason},
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission paused.",
    )


async def handle_mission_resume(
    *,
    session_id: UUID,
    agent_id: str,
    session_store: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.status != "paused":
        return MissionHandlerResult(
            ok=False,
            error="No paused mission on this session.",
        )
    await mission_store.set_status(active.id, "active")
    await session_store.emit_event(
        session_id, EventType.MISSION_RESUMED,
        {"mission_id": str(active.id)},
    )
    # Wake the coordinator so pending continuations are processed.
    await enqueue_session(redis, agent_id, session_id)
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission resumed.",
    )


async def handle_mission_cancel(
    *,
    session_id: UUID,
    reason: str | None,
    cascade_to_workers: bool,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(ok=False, error="No active mission to cancel.")
    if active.status not in ("active", "paused"):
        return MissionHandlerResult(
            ok=False, mission_id=active.id,
            error=f"Mission already terminal (status={active.status}).",
        )
    await mission_store.set_status(
        active.id, "cancelled", cancelled_reason=reason,
    )
    await session_store.clear_session_config_key(session_id, "active_mission_id")
    if cascade_to_workers:
        # Cascade implementation lives in Task 6.
        from surogates.missions.commands import _cascade_cancel_workers
        await _cascade_cancel_workers(
            mission_id=active.id,
            session_factory=session_factory,
            redis=redis,
        )
    await session_store.emit_event(
        session_id, EventType.MISSION_CANCELLED,
        {
            "mission_id": str(active.id),
            "reason": reason,
            "cascade_to_workers": cascade_to_workers,
        },
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission cancelled.",
    )


async def _cascade_cancel_workers(
    *, mission_id: UUID, session_factory: Any, redis: Any,
) -> None:
    """Stub — implemented in Task 6 (per-child cancel_task)."""
    return None
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: 8 PASS (3 from Task 4 + 5 new).

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/commands.py tests/integration/missions/test_commands.py
git commit -m "feat(missions): /mission status + pause + resume + cancel slash handlers"
```

---

## Task 6: Cascade cancel — issue `cancel_task` per non-terminal mission task

**Files:**
- Modify: `surogates/missions/commands.py` (replace `_cascade_cancel_workers` stub with real implementation)
- Modify: `tests/integration/missions/test_commands.py` (append cascade test)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_commands.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_with_cascade_publishes_interrupt_per_running_worker(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """cascade_to_workers=True publishes INTERRUPT_CHANNEL_PREFIX<session_id>
    for each non-terminal task with mission_id == mission."""
    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.db.models import Task
    from surogates.missions.commands import (
        handle_mission_create, handle_mission_cancel,
    )
    from surogates.missions.store import MissionStore

    store = MissionStore(session_factory)
    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )

    # Add two mission tasks, one running (with a session), one ready (no session).
    worker_session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=worker_session_id, org_id=org_id, user_id=user_id,
            agent_id="orchestrator", channel="task", status="active",
        ))
        await db.flush()
        running = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="train", status="running", mission_id=created.mission_id,
            current_session_id=worker_session_id, attempt_count=1,
        )
        ready = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="eval", status="ready", mission_id=created.mission_id,
        )
        done = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="research", status="done", mission_id=created.mission_id,
        )
        db.add_all([running, ready, done])
        await db.commit()

    res = await handle_mission_cancel(
        session_id=chat_session.id,
        reason="abort",
        cascade_to_workers=True,
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    assert res.ok is True

    # Running task got an interrupt on its session's channel.
    channels_published = [c.args[0] for c in redis.publish.call_args_list]
    assert f"{INTERRUPT_CHANNEL_PREFIX}{worker_session_id}" in channels_published

    # Both non-terminal tasks are now 'cancelled'.
    async with session_factory() as db:
        for tid, expected in (
            (running.id, "cancelled"),
            (ready.id, "cancelled"),
            (done.id, "done"),  # done stays done
        ):
            t = await db.get(Task, tid)
            assert t.status == expected
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py::test_cancel_with_cascade_publishes_interrupt_per_running_worker -v
```

Expected: FAIL — current stub does nothing.

- [ ] **Step 3: Replace the stub with the real cascade implementation**

In `surogates/missions/commands.py`, replace `_cascade_cancel_workers`:

```python
async def _cascade_cancel_workers(
    *, mission_id: UUID, session_factory: Any, redis: Any,
) -> None:
    """Cancel every non-terminal task belonging to ``mission_id``.

    For each ``running`` task, publishes an interrupt on its current
    Session's ``INTERRUPT_CHANNEL_PREFIX`` channel (the same mechanism
    ``cancel_task`` / ``stop_worker`` use). For each non-running
    non-terminal task, transitions status to ``cancelled``.
    """
    from sqlalchemy import select as _sel, update as _upd

    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.db.models import Task

    async with session_factory() as db:
        rows = (await db.execute(
            _sel(Task).where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
        )).scalars().all()
        running_session_ids: list[UUID] = []
        for row in rows:
            if row.status == "running" and row.current_session_id is not None:
                running_session_ids.append(row.current_session_id)
        await db.execute(
            _upd(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
            .values(status="cancelled", completed_at=func.now())
        )
        await db.commit()

    for sid in running_session_ids:
        try:
            await redis.publish(
                f"{INTERRUPT_CHANNEL_PREFIX}{sid}", "mission_cancel_cascade",
            )
        except Exception:
            # Don't let one bad publish strand the rest of the cascade.
            # The worker session will time out naturally if the interrupt
            # didn't land; the task is already marked cancelled in the DB.
            pass
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/commands.py tests/integration/missions/test_commands.py
git commit -m "feat(missions): cascade cancel issues interrupts + cancels non-terminal mission tasks"
```

---

## Task 7: `spawn_task` stamps `mission_id` from active mission

**Files:**
- Modify: `surogates/tasks/tools.py` (`_spawn_task_handler` reads `session.config['active_mission_id']`)
- Create: `tests/integration/missions/test_spawn_task_mission_id.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/missions/test_spawn_task_mission_id.py`:

```python
"""When a session has an active mission, spawn_task stamps mission_id."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from surogates.db.models import Session as ORMSession, Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_stamps_mission_id_when_active_mission(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A spawn_task call from a session with active_mission_id sets
    tasks.mission_id on the new row."""
    from surogates.tasks.tools import _spawn_task_handler

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    mission_id = created.mission_id

    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    tenant = MagicMock(org_id=org_id, user_id=user_id)
    result = await _spawn_task_handler(
        {"goal": "research"},
        session_store=session_store, redis=redis, tenant=tenant,
        session_id=str(chat_session.id), session_factory=session_factory,
    )
    parsed = json.loads(result)
    task_id = uuid.UUID(parsed["task_id"])

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.mission_id == mission_id


@pytest.mark.asyncio(loop_scope="session")
async def test_spawn_task_leaves_mission_id_null_for_non_mission_session(
    session_factory, session_store, org_id, user_id,
):
    """A session without active_mission_id produces tasks with mission_id=None."""
    from surogates.tasks.tools import _spawn_task_handler

    sid = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=sid, org_id=org_id, user_id=user_id, agent_id="orchestrator",
            channel="web", status="active",
            config={
                "storage_bucket": "test-bucket",
                "workspace_path": f"/workspace/test/{sid}",
                "supports_vision": False,
            },
        ))
        await db.commit()

    redis = AsyncMock(zadd=AsyncMock(), publish=AsyncMock())
    tenant = MagicMock(org_id=org_id, user_id=user_id)
    result = await _spawn_task_handler(
        {"goal": "research"},
        session_store=session_store, redis=redis, tenant=tenant,
        session_id=str(sid), session_factory=session_factory,
    )
    task_id = uuid.UUID(json.loads(result)["task_id"])

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.mission_id is None
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_spawn_task_mission_id.py -v
```

Expected: First test FAILS (mission_id is None); second test PASSES (current behaviour).

- [ ] **Step 3: Modify `_spawn_task_handler` to read active_mission_id**

In `surogates/tasks/tools.py`, locate the section where the `Task(...)` row is constructed (Phase 1 of `_spawn_task_handler`). Before the `Task(...)` call, fetch the calling session's active_mission_id:

```python
        # Phase 0: read active_mission_id from the calling session so the
        # task inherits the mission scope. Reading session.config from a
        # row is the source of truth (matches /mission's writes).
        from surogates.db.models import Session as _ORMSession
        async with session_factory() as db:
            sess_row = await db.get(_ORMSession, parent_session_id)
            active_mid_str = None
            if sess_row is not None:
                active_mid_str = (sess_row.config or {}).get("active_mission_id")
        active_mission_id = UUID(active_mid_str) if active_mid_str else None
```

Then pass it to the Task constructor:

```python
            task = Task(
                org_id=org_id,
                parent_session_id=parent_session_id,
                agent_def_name=agent_def_name,
                goal=goal_clean,
                context=context,
                status=initial_status,
                max_attempts=max_attempts,
                mission_id=active_mission_id,
            )
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_spawn_task_mission_id.py tests/integration/tasks/ -v
```

Expected: both new tests PASS; existing task layer tests still pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/tasks/tools.py tests/integration/missions/test_spawn_task_mission_id.py
git commit -m "feat(missions): spawn_task stamps mission_id from session.config.active_mission_id"
```

---

## Task 8: Mission evaluator — trigger detection + rate-limit guard

**Files:**
- Create: `surogates/missions/evaluator.py` (trigger logic + rate-limit; prompt building in Task 9)
- Create: `tests/missions/test_evaluator_unit.py`
- Create: `tests/integration/missions/test_evaluator.py` (trigger detection cases here)

- [ ] **Step 1: Write the failing tests**

Create `tests/missions/test_evaluator_unit.py`:

```python
"""Unit tests for the evaluator's pure-function pieces."""
from __future__ import annotations

import pytest


def test_response_contains_completion_marker_positive():
    """A response with [[mission-complete]] on its own line triggers."""
    from surogates.missions.evaluator import response_claims_completion

    body = "I've trained the model.\n\n[[mission-complete]]\n\nLogs attached."
    assert response_claims_completion(body) is True


def test_response_contains_completion_marker_inside_prose_no_trigger():
    """[[mission-complete]] inside running prose does NOT trigger (must be its own line)."""
    from surogates.missions.evaluator import response_claims_completion

    body = "I'll mark this with [[mission-complete]] when I'm done, but not yet."
    assert response_claims_completion(body) is False


def test_response_contains_completion_marker_negative():
    """A regular response does not trigger."""
    from surogates.missions.evaluator import response_claims_completion

    assert response_claims_completion("just regular work output") is False


def test_response_contains_completion_marker_empty():
    from surogates.missions.evaluator import response_claims_completion

    assert response_claims_completion("") is False
    assert response_claims_completion(None) is False
```

Create `tests/integration/missions/test_evaluator.py`:

```python
"""Integration tests for the mission evaluator trigger logic."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from surogates.db.models import Session as ORMSession, Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_trigger_on_task_terminal_event(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A mission task transitioning to done makes should_evaluate return True."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    # Insert a mission task in 'done' state — the trigger condition.
    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="t", status="done",
            mission_id=created.mission_id,
        ))
        await db.commit()

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="I queued some work.",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is True
    assert decision.trigger == "task_terminal"


@pytest.mark.asyncio(loop_scope="session")
async def test_trigger_on_completion_marker(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """An explicit [[mission-complete]] marker triggers evaluation."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="Done.\n[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is True
    assert decision.trigger == "completion_claim"


@pytest.mark.asyncio(loop_scope="session")
async def test_no_trigger_on_plain_response_without_terminal_task(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The /goal rule (every no-tool-call response) must NOT apply here."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="Thinking about how to proceed.",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is False


@pytest.mark.asyncio(loop_scope="session")
async def test_rate_limit_blocks_within_window(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A recently-evaluated mission is skipped even if a trigger fires."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    # Record a fresh evaluation to set last_evaluation_at = now.
    await store.record_evaluation(
        created.mission_id, result="needs_revision",
        explanation="", feedback="",
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=30,
    )
    assert decision.should is False
    assert decision.trigger == "rate_limited"


@pytest.mark.asyncio(loop_scope="session")
async def test_old_terminal_task_does_not_retrigger_after_evaluation(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """A terminal task that was already evaluated does not retrigger forever."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="old verifier", status="done",
            completed_at=datetime.now(timezone.utc),
            mission_id=created.mission_id,
        ))
        await db.commit()

    await store.record_evaluation(
        created.mission_id, result="needs_revision",
        explanation="", feedback="",
    )

    decision = await should_evaluate(
        mission_id=created.mission_id,
        coordinator_last_response="plain coordinator response",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is False
    assert decision.trigger == "no_trigger"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /work/surogates && uv run pytest tests/missions/test_evaluator_unit.py tests/integration/missions/test_evaluator.py -v
```

Expected: ImportError on `surogates.missions.evaluator`.

- [ ] **Step 3: Implement the trigger detection module**

Create `surogates/missions/evaluator.py`:

```python
"""Mission evaluator: trigger detection, prompt building, verdict handling.

This module is the keystone described in the design spec:

* The evaluator does NOT fire on every coordinator no-tool-call response
  (that's `/goal`'s rule; for missions it produces too many calls graded
  over too little new information).
* It DOES fire when a mission-linked task transitions to a terminal
  state (a real workstream change), or when the coordinator emits the
  explicit ``[[mission-complete]]`` marker on its own line.
* It is rate-limited at 30 seconds per mission to bound cost when many
  tasks complete in burst.

Prompt building + verdict handling are in this same module (added by
Task 9 alongside the existing trigger helpers).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

logger = logging.getLogger(__name__)


# Triggers an evaluator pass when present on its own line in the
# coordinator's no-tool-call response.
_COMPLETION_MARKER_RE = re.compile(
    r"(?m)^\s*\[\[\s*mission-complete\s*\]\]\s*$",
)


@dataclass(slots=True)
class EvaluationDecision:
    """Result of :func:`should_evaluate`."""

    should: bool
    trigger: str  # "task_terminal" | "completion_claim" | "rate_limited" | "no_trigger"


def response_claims_completion(response: str | None) -> bool:
    """True iff the response contains ``[[mission-complete]]`` on its
    own line (whitespace allowed).

    The marker must be alone on a line; embedded uses inside running
    prose (e.g. "I'll mark with [[mission-complete]] later") do not
    trigger the evaluator.
    """
    if not response:
        return False
    return _COMPLETION_MARKER_RE.search(response) is not None


async def _has_recent_terminal_task(
    mission_id: UUID, *, session_factory: Any, since: Any | None,
) -> bool:
    """True iff a terminal task has completed since the last evaluation."""
    from surogates.db.models import Task

    async with session_factory() as db:
        stmt = (
            select(Task.id)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("done", "failed", "cancelled")),
            )
            .limit(1)
        )
        if since is not None:
            stmt = stmt.where(Task.completed_at.isnot(None), Task.completed_at > since)
        row = await db.scalar(
            stmt
        )
    return row is not None


async def should_evaluate(
    *,
    mission_id: UUID,
    coordinator_last_response: str | None,
    session_factory: Any,
    mission_store: Any,
    rate_limit_seconds: int = 30,
) -> EvaluationDecision:
    """Decide whether to fire the mission evaluator now.

    Order:
    1. If the mission was evaluated within ``rate_limit_seconds``: skip
       (returns ``rate_limited``).
    2. If a mission-linked task reached a terminal state after
       ``last_evaluation_at``: fire with trigger ``task_terminal``.
    3. If the coordinator's last response contains the completion
       marker: fire with trigger ``completion_claim``.
    4. Otherwise: skip (``no_trigger``).

    The rate-limit check runs first so the cheapest negative path is
    fast (single SELECT on the mission row); only when the limit is
    clear do we run the more expensive task-table lookup.
    """
    if await mission_store.recently_evaluated(
        mission_id, window_seconds=rate_limit_seconds,
    ):
        return EvaluationDecision(should=False, trigger="rate_limited")

    mission = await mission_store.get(mission_id)

    if await _has_recent_terminal_task(
        mission_id,
        session_factory=session_factory,
        since=mission.last_evaluation_at,
    ):
        return EvaluationDecision(should=True, trigger="task_terminal")

    if response_claims_completion(coordinator_last_response):
        return EvaluationDecision(should=True, trigger="completion_claim")

    return EvaluationDecision(should=False, trigger="no_trigger")
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/missions/test_evaluator_unit.py tests/integration/missions/test_evaluator.py -v
```

Expected: 9 PASS (4 unit + 5 integration).

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/evaluator.py tests/missions/test_evaluator_unit.py tests/integration/missions/test_evaluator.py
git commit -m "feat(missions): evaluator trigger detection + rate-limit guard"
```

---

## Task 9: Mission evaluator — prompt building + verdict handling + continuation

**Files:**
- Modify: `surogates/missions/evaluator.py` (append prompt + judge call + verdict handling)
- Modify: `tests/integration/missions/test_evaluator.py` (append verdict cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_evaluator.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_build_evaluator_prompt_includes_all_four_blocks(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """The evaluator prompt carries rubric, response, completed tasks, in-flight tasks."""
    from surogates.missions.evaluator import build_evaluator_prompt

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="train model", rubric="gsm8k >= 0.8",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    # Two completed mission tasks + one running.
    async with session_factory() as db:
        db.add_all([
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="research vLLM", status="done",
                result="vLLM cheaper at our scale",
                result_metadata={"sources": 5},
                mission_id=created.mission_id,
            ),
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="verifier-round-1", status="done",
                result="gsm8k=0.65 over 200 examples",
                result_metadata={"score": 0.65, "n": 200},
                mission_id=created.mission_id,
            ),
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="training-round-2", status="running",
                attempt_count=1, mission_id=created.mission_id,
            ),
        ])
        await db.commit()

    prompt = await build_evaluator_prompt(
        mission_id=created.mission_id,
        coordinator_last_response="Round 1 done; running round 2.",
        session_factory=session_factory,
        mission_store=store,
    )
    assert "gsm8k >= 0.8" in prompt  # rubric
    assert "Round 1 done" in prompt  # response
    assert "vLLM cheaper" in prompt  # completed tasks block
    assert "verifier-round-1" in prompt or "0.65" in str(prompt)
    assert "training-round-2" in prompt  # in-flight tasks block
    assert "running" in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_satisfied_marks_status_terminal(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await apply_verdict(
        mission_id=created.mission_id,
        verdict={"result": "satisfied", "explanation": "rubric met", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "satisfied"
    assert m.last_evaluation_result == "satisfied"
    async with session_factory() as db:
        sess = await db.get(ORMSession, chat_session.id)
        assert "active_mission_id" not in (sess.config or {})


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_needs_revision_emits_continuation(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    await apply_verdict(
        mission_id=created.mission_id,
        verdict={
            "result": "needs_revision",
            "explanation": "verifier shows 0.65, threshold 0.8",
            "feedback": "spawn another training round and a verifier task",
        },
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "active"
    assert m.iteration == 1
    assert m.last_evaluation_result == "needs_revision"
    # mission.continuation event emitted with a synthetic user.message after.
    async with session_factory() as db:
        cont = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.MISSION_CONTINUATION.value,
            )
        )).scalars().all()
        assert len(cont) == 1
        synthetic = (await db.execute(
            select(Event).where(
                Event.session_id == chat_session.id,
                Event.type == EventType.USER_MESSAGE.value,
            )
        )).scalars().all()
        assert any(
            e.data.get("synthetic") == "mission_continuation" for e in synthetic
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_apply_verdict_max_iterations_reached(
    session_factory, session_store, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import apply_verdict

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    # Pre-bump iteration to one below max_iterations.
    for _ in range(19):
        await store.increment_iteration(created.mission_id)

    await apply_verdict(
        mission_id=created.mission_id,
        verdict={"result": "needs_revision", "explanation": "", "feedback": ""},
        coordinator_session_id=chat_session.id,
        session_store=session_store, mission_store=store,
        trigger="task_terminal",
    )
    m = await store.get(created.mission_id)
    assert m.status == "max_iterations_reached"
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_evaluator.py -v
```

Expected: ImportError / AttributeError on `build_evaluator_prompt`, `apply_verdict`.

- [ ] **Step 3: Implement prompt building and verdict handling**

Append to `surogates/missions/evaluator.py`:

```python
import json
from textwrap import dedent

from surogates.session.events import EventType


# Cap on the coordinator response excerpt inserted into the prompt.
_RESPONSE_MAX_CHARS: int = 16_384
_RESULT_MAX_CHARS: int = 400
_TASKS_BLOCK_LIMIT: int = 20


_SYSTEM_PROMPT = dedent("""\
    You are the rubric judge for a Surogates Mission. Read the rubric and
    the structured workstream state, then decide whether the rubric is
    satisfied. Be strict — only return `satisfied` when concrete evidence
    in the completed mission tasks demonstrates the rubric was met
    (typically `result_metadata` from a verifier task).

    Respond with a single JSON object, no prose around it:

        {"result": "satisfied" | "needs_revision" | "blocked" | "failed",
         "explanation": "<1-3 sentences>",
         "feedback": "<actionable feedback for the coordinator if needs_revision; empty otherwise>"}

    Verdict guidance:
    - "satisfied": evidence backs rubric completion.
    - "needs_revision": work is in progress or incomplete; feedback names
      what's missing or wrong.
    - "blocked": the rubric cannot be progressed without external input
      that the coordinator has not yet requested (rare; usually the
      coordinator should call ``task_block`` instead and this verdict
      should be reserved for true dead-ends).
    - "failed": the rubric is unreachable from current state (e.g. data
      is impossible, contradictory rubric).

    Do not honour completion claims in prose alone. The coordinator's
    response may contain `[[mission-complete]]` as a hint that you should
    look closely; the verdict still depends on evidence from the
    completed mission tasks block.
""").strip()


async def build_evaluator_prompt(
    *,
    mission_id: UUID,
    coordinator_last_response: str | None,
    session_factory: Any,
    mission_store: Any,
) -> str:
    """Render the user-side prompt the judge LLM consumes.

    Includes four blocks: rubric, coordinator's latest response,
    completed mission tasks (with result + result_metadata), in-flight
    mission tasks. Each task block is bounded to the most recent
    ``_TASKS_BLOCK_LIMIT`` rows.
    """
    from surogates.db.models import Task

    mission = await mission_store.get(mission_id)
    response_excerpt = (coordinator_last_response or "")[:_RESPONSE_MAX_CHARS]

    async with session_factory() as db:
        completed_rows = (await db.execute(
            select(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status == "done",
            )
            .order_by(Task.completed_at.desc().nulls_last())
            .limit(_TASKS_BLOCK_LIMIT)
        )).scalars().all()
        in_flight_rows = (await db.execute(
            select(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
            .order_by(Task.created_at.desc())
            .limit(_TASKS_BLOCK_LIMIT)
        )).scalars().all()

    def _render_completed(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        lines: list[str] = []
        for t in rows:
            short_id = str(t.id)[:8]
            label = t.agent_def_name or "worker"
            result = (t.result or "")[:_RESULT_MAX_CHARS]
            meta = json.dumps(t.result_metadata) if t.result_metadata else "{}"
            lines.append(
                f"- T{short_id} ({label}): result={result!r}; metadata={meta}"
            )
        return "\n".join(lines)

    def _render_in_flight(rows: list[Any]) -> str:
        if not rows:
            return "(none)"
        lines: list[str] = []
        for t in rows:
            short_id = str(t.id)[:8]
            label = t.agent_def_name or "worker"
            lines.append(
                f"- T{short_id} ({label}): status={t.status}; attempts={t.attempt_count}"
            )
        return "\n".join(lines)

    prompt = dedent("""\
        # Mission rubric

        {rubric}

        # Coordinator's latest response

        {response}

        # Completed mission tasks ({n_done})

        {completed_block}

        # In-flight mission tasks ({n_in_flight})

        {in_flight_block}

        # Verdict

        Return JSON only.
    """).format(
        rubric=mission.rubric,
        response=response_excerpt or "(empty)",
        n_done=len(completed_rows),
        completed_block=_render_completed(completed_rows),
        n_in_flight=len(in_flight_rows),
        in_flight_block=_render_in_flight(in_flight_rows),
    )
    return prompt


def evaluator_system_prompt() -> str:
    """The system message for the judge LLM call."""
    return _SYSTEM_PROMPT


_CONTINUATION_TEMPLATE = dedent("""\
    [Continuing toward your mission]

    Description: {description}

    Rubric:
    {rubric}

    Evaluator verdict: needs_revision
    Evaluator feedback: {feedback}

    Current mission state:
    - {n_done} task(s) completed
    - {n_in_flight} task(s) in flight (running/ready/todo/blocked)
    - Iteration {iteration}/{max_iterations}

    Inspect the mission task tree via ``task_show`` on a recent child if
    you need detail. Then either:
      (a) spawn one or more corrective tasks (via ``spawn_task``) to
          address the evaluator's feedback, OR
      (b) call ``task_block`` on your own session with a question if you
          need human input, OR
      (c) call ``task_complete`` on your own session with a failure
          summary if you believe the rubric cannot be satisfied.

    Do NOT claim completion in prose alone. The evaluator only honours a
    completion claim when a verifier task's result_metadata supports it,
    or when you explicitly mark completion with ``[[mission-complete]]``
    on its own line.
""").strip()


async def apply_verdict(
    *,
    mission_id: UUID,
    verdict: dict[str, Any],
    coordinator_session_id: UUID,
    session_store: Any,
    mission_store: Any,
    trigger: str,
) -> None:
    """Record the evaluator's verdict and act on it.

    Writes the ``last_evaluation_*`` fields, emits the
    ``mission.evaluation.end`` event, then dispatches by verdict:

    * ``satisfied`` / ``blocked`` / ``failed`` → set the matching status
      (terminal).
    * ``needs_revision`` → increment iteration. If at or past
      ``max_iterations`` → status ``max_iterations_reached``. Else
      emit ``mission.continuation`` + a synthetic user.message with the
      continuation prompt so the coordinator wakes with revised guidance.
    """
    result = verdict.get("result", "needs_revision")
    explanation = verdict.get("explanation", "") or ""
    feedback = verdict.get("feedback", "") or ""

    await mission_store.record_evaluation(
        mission_id, result=result, explanation=explanation, feedback=feedback,
    )

    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_EVALUATION_END,
        {
            "mission_id": str(mission_id),
            "trigger": trigger,
            "result": result,
            "explanation": explanation,
            "feedback": feedback,
        },
    )

    if result in ("satisfied", "blocked", "failed"):
        await mission_store.set_status(mission_id, result)
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return

    if result != "needs_revision":
        # Unknown verdict value; treat as needs_revision but log.
        logger.warning(
            "Unknown mission evaluator verdict %r for mission %s; treating as needs_revision",
            result, mission_id,
        )

    new_iter = await mission_store.increment_iteration(mission_id)
    mission = await mission_store.get(mission_id)
    if new_iter >= mission.max_iterations:
        await mission_store.set_status(mission_id, "max_iterations_reached")
        await session_store.clear_session_config_key(
            coordinator_session_id, "active_mission_id",
        )
        return

    # Re-fetch counts to render an up-to-date continuation prompt.
    from sqlalchemy import func as _func

    from surogates.db.models import Task

    async with mission_store._sf() as db:
        n_done = int(await db.scalar(
            select(_func.count(Task.id)).where(
                Task.mission_id == mission_id, Task.status == "done",
            )
        ) or 0)
        n_in_flight = int(await db.scalar(
            select(_func.count(Task.id)).where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
        ) or 0)

    continuation = _CONTINUATION_TEMPLATE.format(
        description=mission.description,
        rubric=mission.rubric,
        feedback=feedback or explanation,
        n_done=n_done,
        n_in_flight=n_in_flight,
        iteration=new_iter,
        max_iterations=mission.max_iterations,
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.MISSION_CONTINUATION,
        {"mission_id": str(mission_id), "iteration": new_iter},
    )
    await session_store.emit_event(
        coordinator_session_id, EventType.USER_MESSAGE,
        {"content": continuation, "synthetic": "mission_continuation"},
    )
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_evaluator.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/missions/evaluator.py tests/integration/missions/test_evaluator.py
git commit -m "feat(missions): evaluator prompt building + verdict handling + continuation"
```

---

## Task 10: Wire the evaluator into the harness loop

**Files:**
- Modify: `surogates/harness/loop.py` (call the mission evaluator after no-tool-call responses, alongside the existing `/goal` outcome evaluator)
- Modify: `tests/integration/missions/test_evaluator.py` (append end-to-end harness wire test)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_evaluator.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_harness_runs_evaluator_when_mission_task_completed(
    session_factory, session_store, org_id, user_id, chat_session,
):
    """End-to-end: with an active mission and a done mission-task, the
    harness's mission evaluator hook fires and records an evaluation."""
    from surogates.db.models import Task
    from surogates.harness.loop import _maybe_run_mission_evaluator
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore
    from unittest.mock import MagicMock

    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="trivial — always satisfied",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="t", status="done", mission_id=created.mission_id,
        ))
        await db.commit()

    # Stub LLM judge that always returns satisfied.
    judge = AsyncMock(return_value={
        "result": "satisfied", "explanation": "ok", "feedback": "",
    })

    await _maybe_run_mission_evaluator(
        session_id=chat_session.id,
        coordinator_last_response="some work",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        judge=judge,
    )

    m = await store.get(created.mission_id)
    assert m.status == "satisfied"
    assert m.last_evaluation_result == "satisfied"
    judge.assert_called_once()
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_evaluator.py::test_harness_runs_evaluator_when_mission_task_completed -v
```

Expected: ImportError on `_maybe_run_mission_evaluator`.

- [ ] **Step 3: Implement the harness hook**

Add to `surogates/harness/loop.py`, near the existing `_evaluate_outcome` function (`loop.py:4011` area). Add the new function:

```python
class MissionJudgeParseError(ValueError):
    """Raised when the mission judge returns non-JSON or malformed JSON."""


async def _maybe_run_mission_evaluator(
    *,
    session_id: UUID,
    coordinator_last_response: str | None,
    session_store: Any,
    session_factory: Any,
    mission_store: Any,
    judge: Any,
) -> None:
    """Run the mission evaluator if the session has an active mission AND
    a trigger condition fires AND the rate limit is clear.

    ``judge`` is an async callable that takes
    ``(system_prompt, user_prompt) -> dict`` and returns the parsed
    verdict JSON. Tests inject a stub; production wires it to the
    auxiliary or base LLM client (see :func:`_run_mission_judge` below).
    """
    from surogates.missions.evaluator import (
        apply_verdict,
        build_evaluator_prompt,
        evaluator_system_prompt,
        should_evaluate,
    )
    from surogates.session.events import EventType

    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.status != "active":
        return

    decision = await should_evaluate(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    if not decision.should:
        return

    await session_store.emit_event(
        session_id, EventType.MISSION_EVALUATION_START,
        {
            "mission_id": str(active.id),
            "iteration": active.iteration,
            "trigger": decision.trigger,
        },
    )

    user_prompt = await build_evaluator_prompt(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    try:
        verdict = await judge(evaluator_system_prompt(), user_prompt)
    except MissionJudgeParseError as exc:
        failures = await mission_store.record_parse_failure(active.id)
        await session_store.emit_event(
            session_id, EventType.MISSION_EVALUATION_END,
            {
                "mission_id": str(active.id),
                "iteration": active.iteration,
                "trigger": decision.trigger,
                "result": "needs_revision",
                "explanation": "judge parse failure",
                "feedback": str(exc)[:500],
                "parse_failed": True,
                "parse_failures": failures,
            },
        )
        return
    except Exception as exc:
        logger.warning(
            "Mission %s evaluator judge call failed: %s", active.id, exc,
        )
        verdict = {
            "result": "needs_revision",
            "explanation": "judge call failed",
            "feedback": str(exc)[:500],
        }

    await apply_verdict(
        mission_id=active.id,
        verdict=verdict,
        coordinator_session_id=session_id,
        session_store=session_store,
        mission_store=mission_store,
        trigger=decision.trigger,
    )
```

Then locate where `_evaluate_outcome` is called in the harness loop (around `loop.py:2132`). Right next to that call, add a parallel mission-evaluator call:

```python
                # /goal outcome evaluator (existing call)
                outcome_result = await self._evaluate_outcome(...)

                # /mission evaluator — fires only when triggered, never
                # on every no-tool-call response.
                try:
                    from surogates.missions.store import MissionStore
                    await _maybe_run_mission_evaluator(
                        session_id=session.id,
                        coordinator_last_response=final_response,
                        session_store=self._store,
                        session_factory=self._session_factory,
                        mission_store=MissionStore(self._session_factory),
                        judge=self._mission_judge,
                    )
                except Exception:
                    logger.exception(
                        "Mission evaluator hook failed for session %s; continuing",
                        session.id,
                    )
```

And add a `_mission_judge` attribute to `AgentHarness.__init__` that wraps the existing evaluator LLM client:

```python
        self._mission_judge = _build_mission_judge(
            llm_client=llm_client,
            outcomes_config=getattr(settings, "outcomes", None),
        )
```

Where `_build_mission_judge` is a small factory that returns an async callable. Reuse the same model/client pattern `_evaluate_outcome` uses today (read `_evaluate_outcome` to mirror — the contract is system_prompt + user_prompt → JSON dict). For initial implementation:

```python
def _build_mission_judge(*, llm_client: Any, outcomes_config: Any) -> Any:
    """Return an async (system_prompt, user_prompt) -> dict callable.

    Mirrors :func:`_evaluate_outcome`'s use of the outcomes-configured
    evaluator model. Falls back to a stub that always returns
    needs_revision when the LLM client is not available (e.g. in some
    test rigs).
    """
    import json
    from surogates.harness.outcomes import build_evaluator_messages_raw  # if needed

    async def judge(system_prompt: str, user_prompt: str) -> dict[str, Any]:
        model = (
            (getattr(outcomes_config, "evaluator_model", None) if outcomes_config else None)
            or "gpt-4o-mini"
        )
        try:
            resp = await llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
            )
            raw = resp.choices[0].message.content
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MissionJudgeParseError(str(exc)) from exc

    return judge
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_evaluator.py -v
```

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py tests/integration/missions/test_evaluator.py
git commit -m "feat(missions): wire evaluator into harness loop after no-tool-call responses"
```

---

## Task 11: REST API — GET endpoints (list, detail, tasks, workers)

**Files:**
- Create: `surogates/api/routes/missions.py`
- Modify: `surogates/api/app.py`
- Create: `tests/integration/missions/test_api.py`

**Correction from review:** these routes must use `tenant: TenantContext = Depends(get_current_tenant)` like the existing API routes. Do not read `request.state.tenant` directly. API tests must issue a bearer token (for example with `tests.integration.inbox_e2e_helpers.create_user_token_session`) and pass `headers=user_session.auth_headers` on every request.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/missions/test_api.py`:

```python
"""Integration tests for the missions REST API."""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from surogates.db.models import Task
from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore
from unittest.mock import AsyncMock


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_detail(
    inbox_app, session_factory, session_store, org_id, user_id, chat_session,
):
    """GET /v1/missions/{id} returns the mission summary."""
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(f"/v1/missions/{created.mission_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(created.mission_id)
    assert body["status"] == "active"
    assert body["iteration"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_get_mission_tasks(
    inbox_app, session_factory, session_store, org_id, user_id, chat_session,
):
    """GET /v1/missions/{id}/tasks returns the mission task DAG."""
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add_all([
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="r1", status="done", mission_id=created.mission_id,
            ),
            Task(
                org_id=org_id, parent_session_id=chat_session.id,
                goal="r2", status="running", mission_id=created.mission_id,
            ),
        ])
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(f"/v1/missions/{created.mission_id}/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tasks"]) == 2
    statuses = sorted(t["status"] for t in body["tasks"])
    assert statuses == ["done", "running"]


@pytest.mark.asyncio(loop_scope="session")
async def test_get_missions_list_filters_by_user_and_status(
    inbox_app, session_factory, session_store, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.get(
            "/v1/missions?status=active",
            params={"agent_id": "orchestrator"},
        )
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["id"] for m in body["missions"]]
    assert str(created.mission_id) in ids
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_api.py -v
```

Expected: 404 / route not registered.

- [ ] **Step 3: Implement the GET routes**

Create `surogates/api/routes/missions.py`:

```python
"""FastAPI routes for the missions REST surface.

Read-only GET endpoints in this module; POST endpoints (pause, resume,
cancel) in the same router added by Task 12.

Auth: routes assume the existing tenant-context middleware extracts
``org_id`` and ``user_id`` from the request. The mission rows are
scoped to those values; cross-tenant access is rejected by the
``_load_mission_authorized`` helper.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from surogates.db.models import (
    Mission as MissionRow,
    Task as TaskRow,
    TaskLink,
    Session as ORMSession,
)
from surogates.missions.models import Mission
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext


router = APIRouter(prefix="/missions")


def _session_factory(request: Request) -> Any:
    """Pull the async_sessionmaker from app state.

    Mirrors the pattern other routes use (see api/routes/sessions.py).
    """
    return request.app.state.session_factory


async def _load_mission_authorized(
    mission_id: UUID, *, session_factory: Any, tenant: TenantContext,
) -> MissionRow:
    """Fetch a mission row and authorize against the request's tenant."""
    async with session_factory() as db:
        row = await db.get(MissionRow, mission_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"mission {mission_id} not found")
        if row.org_id != tenant.org_id or row.user_id != tenant.user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"mission {mission_id} not found")
    return row


@router.get("")
async def list_missions(
    status_filter: str = Query("", alias="status"),
    agent_id: str = Query(""),
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    async with session_factory() as db:
        stmt = (
            select(MissionRow)
            .where(
                MissionRow.org_id == tenant.org_id,
                MissionRow.user_id == tenant.user_id,
            )
            .order_by(MissionRow.created_at.desc())
            .limit(100)
        )
        if statuses:
            stmt = stmt.where(MissionRow.status.in_(statuses))
        if agent_id:
            stmt = stmt.where(MissionRow.agent_id == agent_id)
        rows = (await db.execute(stmt)).scalars().all()
    return {
        "missions": [Mission.model_validate(r).model_dump(mode="json") for r in rows],
    }


@router.get("/{mission_id}")
async def get_mission(
    mission_id: UUID,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    return Mission.model_validate(row).model_dump(mode="json")


@router.get("/{mission_id}/tasks")
async def get_mission_tasks(
    mission_id: UUID,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    async with session_factory() as db:
        tasks = (await db.execute(
            select(TaskRow).where(TaskRow.mission_id == mission_id)
            .order_by(TaskRow.created_at.asc())
        )).scalars().all()
        links = (await db.execute(
            select(TaskLink).where(
                TaskLink.child_id.in_([t.id for t in tasks] or [mission_id]),
            )
        )).scalars().all()
    parent_ids_by_child: dict[str, list[str]] = {}
    for link in links:
        parent_ids_by_child.setdefault(str(link.child_id), []).append(str(link.parent_id))

    payload = []
    for t in tasks:
        payload.append({
            "id": str(t.id),
            "goal": t.goal,
            "status": t.status,
            "attempt_count": t.attempt_count,
            "max_attempts": t.max_attempts,
            "agent_def_name": t.agent_def_name,
            "result": t.result,
            "result_metadata": t.result_metadata,
            "parent_ids": parent_ids_by_child.get(str(t.id), []),
            "current_session_id": str(t.current_session_id) if t.current_session_id else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        })
    return {"tasks": payload}


@router.get("/{mission_id}/workers")
async def get_mission_workers(
    mission_id: UUID,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """Return live/recent worker activity rows for the mission.

    The client derives a human-friendly activity label from the
    `latest_event_*` fields; the server's job is just to expose them.
    """
    await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.db.models import Event

    async with session_factory() as db:
        tasks = (await db.execute(
            select(TaskRow).where(
                TaskRow.mission_id == mission_id,
                TaskRow.current_session_id.isnot(None),
            )
        )).scalars().all()

        workers: list[dict[str, Any]] = []
        for t in tasks:
            sess = await db.get(ORMSession, t.current_session_id)
            if sess is None:
                continue
            latest = (await db.execute(
                select(Event)
                .where(Event.session_id == sess.id)
                .order_by(Event.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            workers.append({
                "task_id": str(t.id),
                "worker_session_id": str(sess.id),
                "agent_def_name": t.agent_def_name,
                "task_status": t.status,
                "session_status": sess.status,
                "latest_event_id": latest.id if latest else None,
                "latest_event_kind": latest.type if latest else None,
                "latest_event_at": latest.created_at.isoformat() if latest and latest.created_at else None,
                "latest_event_summary": json.dumps(latest.data)[:200] if latest and latest.data else None,
                "transcript_url": f"/chat/{sess.id}",
            })
    return {"workers": workers}
```

Register the router in `surogates/api/app.py`. Add `missions` to the route import tuple near the other `surogates.api.routes` imports, then add the include call before the frontend SPA catch-all:

```python
app.include_router(missions.router, prefix="/v1", tags=["missions"])
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_api.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/missions.py surogates/api/app.py tests/integration/missions/test_api.py
git commit -m "feat(missions): REST GET endpoints (list, detail, tasks, workers)"
```

---

## Task 12: REST API — POST endpoints (pause, resume, cancel)

**Files:**
- Modify: `surogates/api/routes/missions.py` (append POST routes)
- Modify: `tests/integration/missions/test_api.py` (append POST cases)

**Correction from review:** POST route tests need the same authenticated `user_session.auth_headers` setup as Task 11. The route implementations should authorize the mission row before invoking command handlers.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/missions/test_api.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_post_pause_transitions_to_paused(
    inbox_app, session_factory, session_store, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/missions/{created.mission_id}/pause",
            json={"reason": "manual"},
        )
    assert resp.status_code == 200
    m = await store.get(created.mission_id)
    assert m.status == "paused"
    assert m.paused_reason == "manual"


@pytest.mark.asyncio(loop_scope="session")
async def test_post_cancel_with_cascade_marks_tasks_cancelled(
    inbox_app, session_factory, session_store, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    created = await handle_mission_create(
        description="d", rubric="r",
        session_id=chat_session.id, user_id=user_id, org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=AsyncMock(zadd=AsyncMock()),
    )
    async with session_factory() as db:
        db.add(Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="t", status="ready", mission_id=created.mission_id,
        ))
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=inbox_app), base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/v1/missions/{created.mission_id}/cancel",
            json={"reason": "abort", "cascade_to_workers": True},
        )
    assert resp.status_code == 200
    m = await store.get(created.mission_id)
    assert m.status == "cancelled"
    async with session_factory() as db:
        tasks = (await db.execute(
            select(Task).where(Task.mission_id == created.mission_id)
        )).scalars().all()
        statuses = [t.status for t in tasks]
        assert "cancelled" in statuses
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_api.py -v
```

Expected: 404 on pause/cancel.

- [ ] **Step 3: Implement the POST routes**

Append to `surogates/api/routes/missions.py`:

```python
from pydantic import BaseModel


class _PauseBody(BaseModel):
    reason: str | None = None


class _CancelBody(BaseModel):
    reason: str | None = None
    cascade_to_workers: bool = False


@router.post("/{mission_id}/pause")
async def pause_mission_endpoint(
    mission_id: UUID,
    body: _PauseBody,
    request: Request,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_pause
    from surogates.missions.store import MissionStore

    session_store = request.app.state.session_store
    result = await handle_mission_pause(
        session_id=row.session_id, reason=body.reason,
        session_store=session_store,
        mission_store=MissionStore(session_factory),
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {"ok": True, "mission_id": str(result.mission_id), "status": "paused"}


@router.post("/{mission_id}/resume")
async def resume_mission_endpoint(
    mission_id: UUID,
    request: Request,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_resume
    from surogates.missions.store import MissionStore

    redis = request.app.state.redis
    session_store = request.app.state.session_store
    result = await handle_mission_resume(
        session_id=row.session_id, agent_id=row.agent_id,
        session_store=session_store,
        mission_store=MissionStore(session_factory),
        redis=redis,
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {"ok": True, "mission_id": str(result.mission_id), "status": "active"}


@router.post("/{mission_id}/cancel")
async def cancel_mission_endpoint(
    mission_id: UUID,
    body: _CancelBody,
    request: Request,
    session_factory: Any = Depends(_session_factory),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    row = await _load_mission_authorized(
        mission_id, session_factory=session_factory, tenant=tenant,
    )
    from surogates.missions.commands import handle_mission_cancel
    from surogates.missions.store import MissionStore

    redis = request.app.state.redis
    session_store = request.app.state.session_store
    result = await handle_mission_cancel(
        session_id=row.session_id, reason=body.reason,
        cascade_to_workers=body.cascade_to_workers,
        session_store=session_store, session_factory=session_factory,
        mission_store=MissionStore(session_factory),
        redis=redis,
    )
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.error)
    return {
        "ok": True, "mission_id": str(result.mission_id),
        "status": "cancelled",
        "cascade_to_workers": body.cascade_to_workers,
    }
```

- [ ] **Step 4: Run tests**

```bash
cd /work/surogates && uv run pytest tests/integration/missions/test_api.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/missions.py tests/integration/missions/test_api.py
git commit -m "feat(missions): REST POST endpoints (pause, resume, cancel + cascade)"
```

---

## Task 13: Frontend mission API client + route registration

**Files:**
- Create: `web/src/types/mission.ts`
- Create: `web/src/api/missions.ts`
- Create: `web/src/app/routes/missions.tsx`
- Create: `web/src/app/routes/mission-detail.tsx`
- Modify: `web/src/app/router.tsx`

- [ ] **Step 1: Add dashboard-facing mission types**

Create `web/src/types/mission.ts`:

```ts
export type MissionStatus =
  | "active"
  | "paused"
  | "satisfied"
  | "blocked"
  | "failed"
  | "cancelled"
  | "max_iterations_reached";

export type MissionSummary = {
  id: string;
  session_id: string;
  agent_id: string;
  description: string;
  rubric: string;
  status: MissionStatus;
  iteration: number;
  max_iterations: number;
  last_evaluation_result: string | null;
  last_evaluation_explanation: string | null;
  last_evaluation_feedback: string | null;
  last_evaluation_at: string | null;
  paused_reason: string | null;
  cancelled_reason: string | null;
  created_at: string;
  updated_at: string;
  task_counts?: Record<string, number>;
};

export type MissionTask = {
  id: string;
  goal: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  agent_def_name: string | null;
  result: string | null;
  result_metadata: Record<string, unknown> | null;
  current_session_id: string | null;
  created_at: string | null;
  completed_at: string | null;
  parent_ids?: string[];
};

export type MissionWorker = {
  task_id: string;
  worker_session_id: string;
  agent_def_name: string | null;
  task_status: string;
  session_status: string;
  latest_event_id: number | null;
  latest_event_kind: string | null;
  latest_event_at: string | null;
  latest_event_summary: string | null;
  transcript_url: string;
};
```

- [ ] **Step 2: Add the REST client**

Create `web/src/api/missions.ts`:

```ts
import { authFetch } from "@/api/auth";
import type { MissionSummary, MissionTask, MissionWorker } from "@/types/mission";

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as T;
}

export async function getMission(missionId: string): Promise<MissionSummary> {
  return readJson<MissionSummary>(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}`),
  );
}

export async function getMissionTasks(missionId: string): Promise<MissionTask[]> {
  const body = await readJson<{ tasks: MissionTask[] }>(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}/tasks`),
  );
  return body.tasks;
}

export async function getMissionWorkers(missionId: string): Promise<MissionWorker[]> {
  const body = await readJson<{ workers: MissionWorker[] }>(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}/workers`),
  );
  return body.workers;
}

export async function pauseMission(missionId: string, reason?: string): Promise<void> {
  await readJson(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}/pause`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason: reason || null }),
    }),
  );
}

export async function resumeMission(missionId: string): Promise<void> {
  await readJson(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}/resume`, {
      method: "POST",
    }),
  );
}

export async function cancelMission(
  missionId: string,
  input: { reason?: string; cascadeToWorkers?: boolean },
): Promise<void> {
  await readJson(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}/cancel`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        reason: input.reason || null,
        cascade_to_workers: input.cascadeToWorkers ?? false,
      }),
    }),
  );
}
```

- [ ] **Step 3: Register mission routes**

Create `web/src/app/routes/missions.tsx`:

```tsx
import { createRoute, Outlet } from "@tanstack/react-router";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

export const missionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/missions",
  beforeLoad: () => requireAuth(),
  component: Outlet,
});
```

Create `web/src/app/routes/mission-detail.tsx`:

```tsx
import { createRoute } from "@tanstack/react-router";
import { lazy } from "react";
import { missionsRoute } from "./missions";

const MissionPage = lazy(() =>
  import("@/features/missions/mission-page").then((m) => ({ default: m.MissionPage })),
);

export const missionDetailRoute = createRoute({
  getParentRoute: () => missionsRoute,
  path: "/$missionId",
  component: MissionPage,
});
```

Modify `web/src/app/router.tsx` to import these routes and add `missionsRoute.addChildren([missionDetailRoute])` to `routeTree`.

- [ ] **Step 4: Typecheck**

```bash
cd /work/surogates/web && npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/types/mission.ts web/src/api/missions.ts web/src/app/routes/missions.tsx web/src/app/routes/mission-detail.tsx web/src/app/router.tsx
git commit -m "feat(web): add mission API client and route"
```

---

## Task 14: Dedicated mission dashboard page

**Files:**
- Create: `web/src/features/missions/mission-page.tsx`

- [ ] **Step 1: Create the dashboard page**

Create `web/src/features/missions/mission-page.tsx` with:
- Header: description, status, rubric excerpt, iteration `iteration/max_iterations`, latest evaluator verdict and feedback.
- Task graph/status area: render mission tasks grouped by status; show dependencies from `parent_ids` when present.
- Live workers panel: render every worker row with derived activity label.
- Evidence panel: render completed task results, pretty-printed `result_metadata`, latest evaluator explanation/feedback, and transcript links.
- Controls: pause, resume, cancel. Cancel opens a confirm dialog with `cascade_to_workers` unchecked by default and the current running-worker count shown inline.
- Polling: refresh mission/tasks/workers every 5 seconds while status is `active` or `paused`; stop when terminal.

Implementation notes:
- Use existing components from `web/src/components/ui/*` (`Button`, `Badge`, `ConfirmDialog`, `Table`, `Tooltip`, etc.) rather than inventing new primitives.
- Use `lucide-react` icons in control buttons.
- Derive worker activity client-side: latest `tool.call` summary first, then latest `llm.response`, otherwise `session_status`.
- Worker transcript links should open `/chat/<worker_session_id>` in a new tab.

- [ ] **Step 2: Add focused frontend tests if the repo has a local test harness**

If a React test runner is present by implementation time, add tests for:
- worker activity label derivation
- cancel dialog payloads (`cascade_to_workers=false` and `true`)
- polling stop on terminal mission status

If no test runner exists, keep the pure label derivation helper in the page module small and verify with `npm run typecheck` plus a browser smoke check.

- [ ] **Step 3: Typecheck and build**

```bash
cd /work/surogates/web && npm run typecheck && npm run build
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add web/src/features/missions/mission-page.tsx
git commit -m "feat(web): add dedicated mission dashboard"
```

---

## Task 15: Skill addendum — `subagent-task-orchestrator` criterion-loop section

**Files:**
- Modify: `skills/kanban/subagent-task-orchestrator/SKILL.md` (append criterion-loop section)

- [ ] **Step 1: No new test** — the SKILL.md change is documentation/prompt content. We verify by reading the file after edit.

- [ ] **Step 2: Append the criterion-loop section to the orchestrator skill**

At the end of `skills/kanban/subagent-task-orchestrator/SKILL.md`, before the closing horizontal rule (if any), append:

```markdown
## Criterion-driven loops (the `/mission` pattern)

When you're the coordinator of a `/mission`, your job has one more step
than a one-shot fan-out: **end each round with a verifier task and
iterate until the rubric is met**.

### How to structure each round

1. Spawn the work tasks for this round (research, training, dataset
   generation — whatever advances the rubric).
2. Spawn a **verifier task** with `parents=[…]` set to every work task
   from this round. The verifier's job is to compute the measurable
   signal the rubric checks (`gsm8k score`, `coverage %`, etc.) and
   write it to `result_metadata` so the mission evaluator can read it
   directly.
3. When the verifier completes, the mission evaluator fires
   automatically (you don't have to call it). It reads the rubric +
   your latest response + the completed-tasks block (where the
   verifier's `result_metadata` lives) and returns one of `satisfied`,
   `needs_revision`, `blocked`, or `failed`.
4. If `needs_revision`: you get a synthetic continuation message
   listing the evaluator's feedback and the current task state. Spawn
   the corrective round; repeat from step 1.

### When you believe the rubric is met

Most of the time, **let the evaluator decide** — emit your normal
end-of-round summary and the evaluator will pick it up via the
task-terminal trigger on the verifier.

When you're SURE the rubric is met and want to short-circuit the
trigger (rare), emit `[[mission-complete]]` on its own line in your
response. The evaluator will fire on the explicit claim and grade the
workstream. **Do not** use this marker as a substitute for evidence —
the evaluator still requires a verifier task's metadata to back the
claim.

### Pitfalls

**Spawning work without a verifier.** If the round has no verifier,
the evaluator's `result_metadata` block is empty and it cannot judge
satisfaction. You'll loop until `max_iterations`. Always end a round
with a measurable signal.

**Spawning the verifier as a peer instead of a child.** The verifier
must list the work tasks as `parents=[…]` so it runs AFTER they
complete. A peer verifier runs in parallel with incomplete work and
produces meaningless numbers.

**Claiming completion in prose.** "I think we're done!" without a
verifier task in `done` state will fail evaluation. The judge ignores
prose-only completion claims.

**Giving up too early.** The evaluator gives you `max_iterations` (20
by default). Use them. If you're stuck after 3-4 rounds, call
`task_block` on your own session — describe what you've tried and what
external input would help. The user (or another agent) can answer and
unblock.

**Giving up too late.** If the rubric is fundamentally unreachable
(contradictory criteria, missing data, infeasible target), call
`task_complete` on your own session with a failure summary in
`result`. The evaluator will read this as evidence and return `failed`.
```

- [ ] **Step 3: Verify the file reads cleanly**

```bash
cd /work/surogates && cat skills/kanban/subagent-task-orchestrator/SKILL.md | wc -l
# Should now be ~250 lines (was ~200 before the addendum).
```

- [ ] **Step 4: Run backend and frontend regression checks**

```bash
cd /work/surogates && uv run pytest tests/missions/ tests/integration/missions/ tests/tasks/ tests/integration/tasks/ tests/test_coordinator.py tests/test_delegate.py tests/test_agent_type_spawn.py
cd /work/surogates/web && npm run typecheck && npm run build
```

Expected: all pass. The exact count depends on how many tests landed across all 15 tasks (~180+ new + existing regression).

- [ ] **Step 5: Commit**

```bash
git add skills/kanban/subagent-task-orchestrator/SKILL.md
git commit -m "docs(skills): subagent-task-orchestrator criterion-driven loop section for /mission"
```

---

## Post-implementation

After all 15 tasks land:

1. **Docs**: write a `docs/missions/index.md` chapter mirroring the format of `docs/tasks/index.md` — same anchors (concepts, state machine, tools, dispatcher, events, multi-tenancy, decision rule). Add a corresponding `### [11b. Missions]` entry in `docs/index.md`. This is a documentation pass, separable from the implementation, but should land in the same PR series so the feature is discoverable.

2. **Migration in prod**: the retrofit DDL in `observability.sql` adds `missions` and `tasks.mission_id` idempotently. For existing prod deploys, ensure `apply_observability_ddl` runs at next startup.

3. **Default orchestrator AgentDef**: decide whether a platform-level orchestrator AgentDef is still needed after the current-session coordinator flow lands. Do not add this to v1 unless `/mission` create needs an explicit target outside the existing `agent_id` session context.

4. **Verify the evaluator's rate limit holds in burst.** When many child tasks complete in a tight window (e.g. a fleet of N verifiers all finishing within 1s), the evaluator should fire at most once per mission per 30s. Add a fuzz-style test if you see issues in early use.

---

## Self-Review Checklist (run after writing this plan)

- ✅ **Spec coverage**: every §section of the design maps to one or more tasks above.
  - Persistent Mission State → Task 1
  - `/mission` Builtin Command → Tasks 3, 4, 5
  - Mission Evaluator (timing, inputs, verdicts, continuation) → Tasks 8, 9
  - Pause and Cancel Semantics → Tasks 5, 6
  - Mission APIs (GET) → Task 11
  - Mission APIs (POST) → Task 12
  - Dashboard → Tasks 13, 14
  - Failure Handling → Tasks 4 (mutual exclusion), 6 (cascade), 8 (rate limit), 9 (parse failure, max-iter)
  - subagent-task-orchestrator skill addendum → Task 15

- ✅ **No placeholders**: every step has concrete files, commands, and expected outcomes.

- ✅ **Type consistency**: `mission_id` (UUID) is used uniformly; `MissionStatus` values match between Pydantic and DB; `MissionHandlerResult` carries `ok`, `mission_id`, `message`, `error` consistently across all five handlers in Tasks 4-6.

- ⚠️ **One known integration verification**: `app.state.session_factory`, `app.state.session_store`, `app.state.redis` — Task 11 + 12 assume these are wired in app startup. Verify against `surogates/api/app.py` at implementation time; if names differ, adjust the route Depends() accordingly.
