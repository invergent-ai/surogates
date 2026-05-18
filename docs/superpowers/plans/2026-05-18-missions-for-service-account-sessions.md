# Missions for Service-Account Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `/mission` to work for chat sessions created through the Surogate Ops Work UI, which authenticate to surogates as per-user service accounts (`user_id=NULL`, `service_account_id=<sa>`) and therefore can never own a mission today.

**Architecture:** Drop the `NOT NULL` constraint on `missions.user_id`, add a nullable `service_account_id` column with FK to `service_accounts`, and update the principal-resolution code path so missions can be owned by either a user OR a service account. The harness, mission store, REST API, ops forwarder, and SDK types all switch from a `user_id`-only authorization predicate to a "principal" predicate (`user_id` if set, else `service_account_id`). A CHECK constraint enforces that exactly one of the two is set, mirroring the principal-shape invariant already documented in `TenantContext`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async ORM, PostgreSQL (asyncpg), pytest-asyncio, React/TypeScript frontend, `@invergent/agent-chat-react` SDK.

**Why no Alembic in surogates:** Surogates does not have an Alembic migration system. Schema changes are applied via `Base.metadata.create_all` (for fresh DBs) plus the idempotent `observability.sql` script (for retrofitting existing DBs). All DDL in this plan goes into `observability.sql`.

## Progress

- [x] Task 1: Schema + DDL retrofit (surogates models + observability.sql)
- [x] Task 2: Pydantic Mission model
- [x] Task 3: MissionStore.create accepts either principal (TDD)
- [x] Task 4: handle_mission_create signature
- [x] Task 5: Harness loop /mission gating
- [x] Task 6: Surogates mission REST API principal-aware auth
- [x] Task 7: SA-principal end-to-end integration test
- [x] Task 8: SurogatesClient (ops) mission queries by principal
- [x] Task 9: Ops mission routes resolve SA principal
- [x] Task 10: SDK + ops frontend wire shape

---

## File Structure

**surogates package — `/work/surogates/`:**
- Modify: `surogates/db/models.py` — Mission ORM model: nullable `user_id`, new `service_account_id`, updated index, CHECK constraint
- Modify: `surogates/db/observability.sql` — update the missions `CREATE TABLE` fallback plus idempotent ALTER/index/constraint retrofit for existing PROD DBs
- Modify: `surogates/missions/models.py` — Pydantic: `user_id Optional`, add `service_account_id Optional`
- Modify: `surogates/missions/store.py` — `create()` accepts `user_id | service_account_id`
- Modify: `surogates/missions/commands.py` — `handle_mission_create` accepts both
- Modify: `surogates/harness/loop.py` — rewrite `/mission` gating + pass principal
- Modify: `surogates/api/routes/missions.py` — principal-aware authorization predicate
- Modify: `surogates/sdk/agent-chat-react/src/types.ts` — nullable `userId`, optional `serviceAccountId`
- Create: `surogates/tests/integration/missions/test_service_account_mission.py` — end-to-end SA-owned mission

**ops package — `/work/surogate-ops/`:**
- Modify: `surogate_ops/core/surogates_client.py` — mission queries accept principal
- Modify: `surogate_ops/server/routes/missions.py` — resolve the Work-chat SA principal and forwarding credentials
- Modify: `frontend/src/features/work/work-agent-chat-adapter.ts` — `MissionRowWire.user_id` nullable, `service_account_id` optional

---

### Task 1: Database schema — Mission model + DDL retrofit

**Goal:** Make `missions.user_id` nullable, add `service_account_id` FK, replace the user-scoped index with a principal-scoped one, and add a `CHECK` constraint that guarantees exactly one of the two ids is set.

**Files:**
- Modify: `surogates/db/models.py:1000-1059`
- Modify: `surogates/db/observability.sql` (edit the existing "Mission layer" block)

- [ ] **Step 1: Update the `Mission` ORM model**

Edit `surogates/db/models.py` — add `CheckConstraint` to the SQLAlchemy import list, then replace the `__table_args__` and `user_id` declaration in the `Mission` class:

```python
class Mission(Base):
    """A durable orchestrated objective with rubric-judged completion."""

    __tablename__ = "missions"
    __table_args__ = (
        Index("idx_missions_session", "session_id"),
        Index(
            "idx_missions_principal_agent_status",
            "org_id", "user_id", "service_account_id", "agent_id", "status",
        ),
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_missions_one_principal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    # ... rest unchanged (agent_id, description, rubric, etc.)
```

`CheckConstraint` is not imported in the current file; add it beside the existing SQLAlchemy imports (`ForeignKey`, `Index`, etc.).

- [ ] **Step 2: Update `observability.sql` mission DDL**

In the existing "Mission layer (orchestrated goals) — retrofits" block, update the `CREATE TABLE IF NOT EXISTS missions` fallback so fresh databases created by this SQL path get the final shape:

```sql
CREATE TABLE IF NOT EXISTS missions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                      uuid NOT NULL REFERENCES orgs(id),
    user_id                     uuid REFERENCES users(id),
    service_account_id          uuid REFERENCES service_accounts(id),
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
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_missions_one_principal
        CHECK ((user_id IS NOT NULL)::int
             + (service_account_id IS NOT NULL)::int = 1)
);
```

Immediately after that `CREATE TABLE`, add the retrofit ALTER/index/constraint statements for already-existing deployments:

```sql
-- ----------------------------------------------------------------------------
-- Missions — relax single-principal ownership.  ``user_id`` was NOT NULL with
-- a FK to users(id) — only user JWTs could own missions.  Sessions created
-- through ops's Work UI authenticate as per-user service accounts (the
-- surogates session row has user_id=NULL, service_account_id=<sa>), so they
-- could never start a /mission.
--
-- Drop NOT NULL on user_id, add service_account_id, and enforce the
-- principal invariant (exactly one of the two is set) via CHECK.
-- ----------------------------------------------------------------------------

ALTER TABLE missions
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS service_account_id uuid
        REFERENCES service_accounts(id);

-- Replace the old (org, user, agent, status) index with one that covers
-- both principal shapes.  Old index is dropped explicitly because the
-- name is changing.
DROP INDEX IF EXISTS idx_missions_user_agent_status;
CREATE INDEX IF NOT EXISTS idx_missions_principal_agent_status
    ON missions (org_id, user_id, service_account_id, agent_id, status);

-- Exactly one principal must be set.  Wrapped in DO block so the ADD is
-- idempotent (PG has no ADD CONSTRAINT IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_missions_one_principal'
          AND conrelid = 'missions'::regclass
    ) THEN
        ALTER TABLE missions
            ADD CONSTRAINT ck_missions_one_principal
            CHECK ((user_id IS NOT NULL)::int
                 + (service_account_id IS NOT NULL)::int = 1);
    END IF;
END $$;
```

- [ ] **Step 3: Verify DDL applies cleanly to a local DB**

Run against your local surogates DB (use the URL from `~/.surogate/config.yaml`'s `surogates_database_url`):

```bash
psql "$SUROGATES_DATABASE_URL" -c "\d missions"
```

Expected (before fix): `user_id | uuid | not null`
Run migrations: `surogate-ops migrate upgrade`
Expected (after fix): `user_id | uuid` (no `not null`), new `service_account_id | uuid` column, new check constraint listed under "Check constraints".

- [ ] **Step 4: Commit**

```bash
cd /work/surogates
git add surogates/db/models.py surogates/db/observability.sql
git commit -m "feat(missions): allow service-account principal ownership

Drop NOT NULL on missions.user_id, add nullable service_account_id with
FK to service_accounts. CHECK constraint enforces the one-principal
invariant.

Idempotent ALTERs added to observability.sql so existing PROD DBs are
retrofitted on next surogate-ops migrate upgrade."
```

---

### Task 2: Pydantic mission model

**Files:**
- Modify: `surogates/missions/models.py:47-70`

- [ ] **Step 1: Make `user_id` optional, add `service_account_id`**

Edit `surogates/missions/models.py` — replace the `Mission` class fields:

```python
class Mission(BaseModel):
    """Snapshot of a Mission row."""

    model_config = {"from_attributes": True}

    id: UUID
    org_id: UUID
    user_id: UUID | None = None
    service_account_id: UUID | None = None
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

- [ ] **Step 2: Commit**

```bash
git add surogates/missions/models.py
git commit -m "feat(missions): nullable user_id, add service_account_id to Mission model"
```

---

### Task 3: MissionStore.create — accept either principal

**Files:**
- Modify: `surogates/missions/store.py:48-89`
- Test: `surogates/tests/integration/missions/test_commands.py` (add a new test alongside existing ones)

- [ ] **Step 1: Write a failing test for service-account-owned mission creation**

Append to `surogates/tests/integration/missions/test_commands.py`:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_create_with_service_account_principal(
    session_factory, session_store, org_id, service_account_id,
    sa_chat_session,
):
    """A mission created from a service-account session is persisted with
    service_account_id (not user_id), and the active_mission_id config
    write still propagates."""
    from surogates.missions.commands import handle_mission_create
    from surogates.missions.store import MissionStore

    redis = AsyncMock()
    redis.zadd = AsyncMock()

    store = MissionStore(session_factory)
    result = await handle_mission_create(
        description="Audit failing CI jobs",
        rubric="Every red job is triaged with a ticket link.",
        session_id=sa_chat_session.id,
        user_id=None,
        service_account_id=service_account_id,
        org_id=org_id,
        agent_id="orchestrator",
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        redis=redis,
    )

    assert result.ok is True
    m = await store.get(result.mission_id)
    assert m.user_id is None
    assert m.service_account_id == service_account_id
```

You will need fixtures `service_account_id` and `sa_chat_session`. Add them to the existing `conftest.py` for the missions test package (`surogates/tests/integration/missions/conftest.py`):

```python
@pytest_asyncio.fixture(loop_scope="session")
async def service_account_id(session_factory, org_id):
    """Create a valid service account row and return its UUID."""
    from tests.integration.conftest import issue_service_account_token

    issued = await issue_service_account_token(
        session_factory, org_id, name="mission-test-sa",
    )
    return issued.id


@pytest_asyncio.fixture(loop_scope="session")
async def sa_chat_session(session_factory, org_id, service_account_id):
    """Insert a service-account-owned chat session and return the row."""
    from surogates.db.models import Session as ORMSession

    sid = uuid.uuid4()
    async with session_factory() as db:
        sess = ORMSession(
            id=sid, org_id=org_id, user_id=None,
            service_account_id=service_account_id,
            agent_id="orchestrator", channel="api",
            model="gpt-4o",
            config=_session_workspace_config(sid),
            status="active",
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess
```

Use `pytest_asyncio` and the existing module-level `uuid` import already present in `surogates/tests/integration/missions/conftest.py`; inserting `ServiceAccount` directly without `token_hash` and `token_prefix` violates NOT NULL constraints.

- [ ] **Step 2: Run the test, confirm it fails**

```bash
cd /work/surogates
uv run pytest tests/integration/missions/test_commands.py::test_create_with_service_account_principal -v
```

Expected: FAIL — `handle_mission_create` does not accept `service_account_id` kwarg yet.

- [ ] **Step 3: Update `MissionStore.create`**

Edit `surogates/missions/store.py:48-89`:

```python
    async def create(
        self,
        *,
        org_id: UUID,
        session_id: UUID,
        agent_id: str,
        description: str,
        rubric: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        max_iterations: int = 20,
    ) -> UUID:
        """Insert a new mission with status='active'.

        Exactly one of ``user_id`` / ``service_account_id`` must be set —
        the DB CHECK constraint enforces it, but reject here too so the
        error surfaces as a clean ValueError instead of an IntegrityError.

        Rejects with :class:`ActiveMissionConflictError` if any mission
        with ``session_id`` is already in ``active`` or ``paused``.
        """
        if (user_id is None) == (service_account_id is None):
            raise ValueError(
                "MissionStore.create requires exactly one of user_id / "
                "service_account_id",
            )
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
                service_account_id=service_account_id,
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
```

- [ ] **Step 4: Commit**

```bash
git add surogates/missions/store.py \
        surogates/tests/integration/missions/test_commands.py \
        surogates/tests/integration/missions/conftest.py
git commit -m "feat(missions): MissionStore.create accepts service_account_id principal"
```

---

### Task 4: handle_mission_create — pass principal through

**Files:**
- Modify: `surogates/missions/commands.py:150-200`

- [ ] **Step 1: Update the function signature and the inner `mission_store.create` call**

Edit `surogates/missions/commands.py` — replace the `handle_mission_create` signature and the `mission_store.create` call:

```python
async def handle_mission_create(
    *,
    description: str,
    rubric: str,
    session_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    redis: Any,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
) -> MissionHandlerResult:
    """Create a new mission on the calling session.

    Exactly one of ``user_id`` / ``service_account_id`` must be set —
    the caller (the harness loop) checks the session principal before
    calling.
    ...
    """
    if (user_id is None) == (service_account_id is None):
        return MissionHandlerResult(
            ok=False,
            error=(
                "handle_mission_create requires exactly one of user_id / "
                "service_account_id"
            ),
        )
    # ... existing _outcome_is_active guard unchanged ...

    try:
        mission_id = await mission_store.create(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            session_id=session_id,
            agent_id=agent_id,
            description=description,
            rubric=rubric,
        )
    except ActiveMissionConflictError as exc:
        return MissionHandlerResult(ok=False, error=str(exc))

    # ... rest of function unchanged ...
```

- [ ] **Step 2: Run the test from Task 3 and confirm it passes**

```bash
cd /work/surogates
uv run pytest tests/integration/missions/test_commands.py::test_create_with_service_account_principal -v
```

Expected: PASS.

- [ ] **Step 3: Run the full missions test suite to confirm no regression**

```bash
uv run pytest tests/integration/missions/ -v
```

Expected: all PASS. The existing user-owned tests should still work because both `user_id` and `service_account_id` default to `None`, and the existing callers pass `user_id=` explicitly.

- [ ] **Step 4: Commit**

```bash
git add surogates/missions/commands.py
git commit -m "feat(missions): handle_mission_create forwards service_account_id principal"
```

---

### Task 5: Harness loop — pass session principal to `/mission`

**Files:**
- Modify: `surogates/harness/loop.py:4108-4137`

- [ ] **Step 1: Replace the `tenant.user_id is None` gate with a principal resolution**

Edit `surogates/harness/loop.py` around line 4108. Replace the existing block:

```python
                if command.action == "create":
                    if redis_client is None:
                        message = (
                            "/mission create cannot run without a Redis "
                            "connection (the coordinator must be enqueued "
                            "after kickoff)."
                        )
                    elif self._tenant.user_id is None:
                        # Service accounts and channel sessions don't have
                        # a user_id; ``missions.user_id`` is NOT NULL.
                        # Reject with a friendly message instead of letting
                        # the insert fail with NotNullViolationError.
                        message = (
                            "/mission requires a user session — service "
                            "accounts and channel principals cannot own "
                            "missions."
                        )
                    else:
                        result = await handle_mission_create(
                            description=command.description or "",
                            rubric=command.rubric or "",
                            session_id=session.id,
                            user_id=self._tenant.user_id,
                            org_id=self._tenant.org_id,
                            agent_id=session.agent_id,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                        # ... if result.ok branch unchanged ...
```

With:

```python
                if command.action == "create":
                    principal_user_id = self._tenant.user_id
                    principal_sa_id = self._tenant.service_account_id
                    if redis_client is None:
                        message = (
                            "/mission create cannot run without a Redis "
                            "connection (the coordinator must be enqueued "
                            "after kickoff)."
                        )
                    elif principal_user_id is None and principal_sa_id is None:
                        # Anonymous-channel sessions have neither a user nor
                        # a service-account principal — the session itself is
                        # the principal.  Missions need a durable owner that
                        # outlives the session, so reject these explicitly.
                        message = (
                            "/mission requires a user or service-account "
                            "session — anonymous channel sessions cannot "
                            "own missions."
                        )
                    else:
                        result = await handle_mission_create(
                            description=command.description or "",
                            rubric=command.rubric or "",
                            session_id=session.id,
                            user_id=principal_user_id,
                            service_account_id=principal_sa_id,
                            org_id=self._tenant.org_id,
                            agent_id=session.agent_id,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                        # ... if result.ok branch unchanged ...
```

(Leave the success branch — the `cfg["active_mission_id"]` block — exactly as it is.)

- [ ] **Step 2: Run existing loop integration tests**

```bash
cd /work/surogates
uv run pytest tests/integration/missions/test_commands.py -v
```

Expected: all PASS — the existing user-principal path still works, the new service-account path works.

- [ ] **Step 3: Commit**

```bash
git add surogates/harness/loop.py
git commit -m "feat(missions): harness /mission accepts service-account principal"
```

---

### Task 6: Surogates mission REST API — principal-aware authorization

**Files:**
- Modify: `surogates/api/routes/missions.py:51-103`

- [ ] **Step 1: Update `_load_mission_authorized` and the `list_missions` filter**

Edit `surogates/api/routes/missions.py` — replace `_load_mission_authorized` (lines ~51-69) with:

```python
def _principal_owns(row: MissionRow, tenant: TenantContext) -> bool:
    """Return True iff the mission belongs to the tenant's principal.

    User-principal tenants match rows where ``user_id == tenant.user_id``.
    Service-account-principal tenants match rows where
    ``service_account_id == tenant.service_account_id``.  Anonymous-channel
    tenants (no user, no SA) match no row — they can't own missions.
    """
    if row.org_id != tenant.org_id:
        return False
    if tenant.user_id is not None:
        return row.user_id == tenant.user_id
    if tenant.service_account_id is not None:
        return row.service_account_id == tenant.service_account_id
    return False


async def _load_mission_authorized(
    mission_id: UUID,
    *,
    session_factory: async_sessionmaker,
    tenant: TenantContext,
) -> MissionRow:
    """Fetch a mission row and authorize against the request's tenant."""
    async with session_factory() as db:
        row = await db.get(MissionRow, mission_id)
        if row is None or not _principal_owns(row, tenant):
            # 404 (not 403) so cross-tenant probes can't confirm existence.
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"mission {mission_id} not found",
            )
    return row
```

Then update the `list_missions` route (lines ~72-103) — replace the WHERE clause:

```python
@router.get("")
async def list_missions(
    status_filter: str = Query("", alias="status"),
    agent_id: str = Query(""),
    session_factory: async_sessionmaker = Depends(_session_factory_dep),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """List the caller's missions, newest first.

    ``status`` is a comma-separated allowlist (e.g. ``active,paused``).
    """
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    principal_filter = _principal_where_clause(tenant)
    if principal_filter is None:
        # Anonymous-channel tenant — no owned missions possible.
        return {"missions": []}
    async with session_factory() as db:
        stmt = (
            select(MissionRow)
            .where(
                MissionRow.org_id == tenant.org_id,
                principal_filter,
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
        "missions": [
            Mission.model_validate(r).model_dump(mode="json") for r in rows
        ],
    }
```

Add this helper above the routes (alongside `_principal_owns`):

```python
def _principal_where_clause(tenant: TenantContext):
    """Return a SQLAlchemy predicate matching missions owned by the tenant.

    None when the tenant has no owning principal (channel session).
    """
    if tenant.user_id is not None:
        return MissionRow.user_id == tenant.user_id
    if tenant.service_account_id is not None:
        return MissionRow.service_account_id == tenant.service_account_id
    return None
```

- [ ] **Step 2: Run existing mission API tests**

```bash
cd /work/surogates
uv run pytest tests/integration/missions/test_api.py -v
```

Expected: all PASS — the user-principal path is unchanged.

- [ ] **Step 3: Commit**

```bash
git add surogates/api/routes/missions.py
git commit -m "feat(missions): mission REST API filters by tenant principal (user or SA)"
```

---

### Task 7: New integration test — SA principal end-to-end via the REST API

**Files:**
- Create: `surogates/tests/integration/missions/test_service_account_mission.py`

- [ ] **Step 1: Write the test**

```python
"""SA-principal /mission flow: a service-account-owned session can
create, list, and get its mission through the REST API."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from surogates.missions.commands import handle_mission_create
from surogates.missions.store import MissionStore


@pytest.mark.asyncio(loop_scope="session")
async def test_sa_principal_can_create_and_read_mission(
    session_factory, session_store, org_id, service_account_id,
    sa_chat_session, sa_authed_client: AsyncClient,
):
    """A mission inserted under an SA principal is retrievable through
    /v1/missions and /v1/missions/{id} when the request carries the
    same SA token."""
    redis = AsyncMock()
    redis.zadd = AsyncMock()

    store = MissionStore(session_factory)
    create_result = await handle_mission_create(
        description="Reconcile failing CI jobs",
        rubric="Every red job has a ticket and an owner.",
        session_id=sa_chat_session.id,
        service_account_id=service_account_id,
        org_id=org_id,
        agent_id=sa_chat_session.agent_id,
        session_store=session_store,
        session_factory=session_factory,
        mission_store=store,
        redis=redis,
    )
    assert create_result.ok is True
    mid = str(create_result.mission_id)

    # GET /v1/missions/{id}
    detail = await sa_authed_client.get(f"/v1/missions/{mid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == mid
    assert body["user_id"] is None
    assert body["service_account_id"] == str(service_account_id)

    # GET /v1/missions (listing scoped to SA principal)
    listing = await sa_authed_client.get("/v1/missions")
    assert listing.status_code == 200
    ids = [m["id"] for m in listing.json()["missions"]]
    assert mid in ids


@pytest.mark.asyncio(loop_scope="session")
async def test_user_principal_cannot_see_sa_owned_mission(
    session_factory, session_store, org_id, service_account_id,
    sa_chat_session, user_authed_client: AsyncClient,
):
    """A user-principal tenant must not see a mission owned by a
    different principal in the same org — even with a known id."""
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    store = MissionStore(session_factory)
    result = await handle_mission_create(
        description="x", rubric="y",
        session_id=sa_chat_session.id,
        service_account_id=service_account_id, org_id=org_id,
        agent_id=sa_chat_session.agent_id,
        session_store=session_store, session_factory=session_factory,
        mission_store=store, redis=redis,
    )
    detail = await user_authed_client.get(f"/v1/missions/{result.mission_id}")
    assert detail.status_code == 404
```

Note: `sa_authed_client` and `user_authed_client` fixtures need to issue requests with the appropriate JWT. Add these to `surogates/tests/integration/missions/conftest.py` (and import `ASGITransport` / `AsyncClient` there):

```python
@pytest_asyncio.fixture(loop_scope="session")
async def sa_authed_client(inbox_app, service_account_id, org_id, sa_chat_session):
    """AsyncClient authenticated as the SA session principal."""
    from surogates.tenant.auth.jwt import create_service_account_session_token

    token = create_service_account_session_token(
        org_id=org_id,
        service_account_id=service_account_id,
        session_id=sa_chat_session.id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def user_authed_client(inbox_app, session_factory, org_id):
    """AsyncClient authenticated as a same-org user principal."""
    from surogates.tenant.auth.jwt import create_access_token
    from tests.integration.conftest import create_user

    user_id = await create_user(session_factory, org_id)
    token = create_access_token(
        org_id, user_id, {"sessions:read", "sessions:write"},
    )
    async with AsyncClient(
        transport=ASGITransport(app=inbox_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client
```

Do not use a bare `surg_sk_...` service-account token for `/v1/missions`; middleware only permits bare service-account tokens on `/v1/api/*`. The route needs a `service_account_session` JWT minted with `create_service_account_session_token`.

- [ ] **Step 2: Run the test**

```bash
cd /work/surogates
uv run pytest tests/integration/missions/test_service_account_mission.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add surogates/tests/integration/missions/test_service_account_mission.py \
        surogates/tests/integration/missions/conftest.py
git commit -m "test(missions): SA-principal end-to-end coverage via REST API"
```

---

### Task 8: SurogatesClient (ops) — mission queries by principal

**Files:**
- Modify: `surogate_ops/core/surogates_client.py:945-1158`

- [ ] **Step 1: Add `service_account_id` to `_mission_to_dict`**

Edit `surogate_ops/core/surogates_client.py` — extend `_mission_to_dict` to include the new column:

```python
    @staticmethod
    def _mission_to_dict(row: MissionRow) -> dict:
        return {
            "id": str(row.id),
            "org_id": str(row.org_id),
            "user_id": str(row.user_id) if row.user_id else None,
            "service_account_id": (
                str(row.service_account_id) if row.service_account_id else None
            ),
            "session_id": str(row.session_id),
            "agent_id": row.agent_id,
            # ... rest unchanged ...
        }
```

- [ ] **Step 2: Update each query method to accept either principal**

Replace the four query methods (`list_missions`, `get_mission`, `list_mission_tasks`, `list_mission_workers`). Pattern: drop the required `user_id: UUID` kwarg and replace with `user_id: UUID | None = None, service_account_id: UUID | None = None`, then build the WHERE clause accordingly.

For `list_missions`:

```python
    async def list_missions(
        self,
        *,
        agent_id: str,
        org_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return missions for the (org, principal, agent) triple.

        Exactly one of ``user_id`` / ``service_account_id`` must be set —
        mirrors the surogates principal invariant.
        """
        if (user_id is None) == (service_account_id is None):
            raise ValueError(
                "list_missions requires exactly one of user_id / "
                "service_account_id",
            )
        statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
        filters = [
            MissionRow.org_id == org_id,
            MissionRow.agent_id == agent_id,
        ]
        if user_id is not None:
            filters.append(MissionRow.user_id == user_id)
        else:
            filters.append(MissionRow.service_account_id == service_account_id)
        if statuses:
            filters.append(MissionRow.status.in_(statuses))
        stmt = (
            select(MissionRow)
            .where(*filters)
            .order_by(MissionRow.created_at.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [self._mission_to_dict(r) for r in rows]
```

For `get_mission`, `list_mission_tasks`, `list_mission_workers` — same pattern: accept both kwargs, validate XOR, and replace the row visibility predicate:

```python
        if user_id is not None:
            principal_match = row.user_id == user_id
        else:
            principal_match = row.service_account_id == service_account_id
        if (
            row.org_id != org_id
            or not principal_match
            or row.agent_id != agent_id
        ):
            return None
```

- [ ] **Step 3: Update imports in `surogate_ops/core/surogates_client.py`**

The Mission model already imports from `surogates.db.models`; nothing new needed unless `MissionRow.service_account_id` triggers a model reload. Verify with:

```bash
cd /work/surogate-ops
uv run python -c "from surogates.db.models import Mission; print(Mission.__table__.columns.keys())"
```

Expected: list includes both `user_id` and `service_account_id`.

- [ ] **Step 4: Commit**

```bash
cd /work/surogate-ops
git add surogate_ops/core/surogates_client.py
git commit -m "feat(missions): SurogatesClient mission queries accept either principal"
```

---

### Task 9: Ops mission routes — resolve principal and forward

**Files:**
- Modify: `surogate_ops/server/routes/missions.py:63-282`

- [ ] **Step 1: Add a mission context resolver helper**

Edit `surogate_ops/server/routes/missions.py` — import `_ensure_ops_chat_service_account` from `.sessions`, remove the no-longer-used `_ops_chat_credential_name`, `_ops_chat_service_account_name`, and `_resolve_current_ops_user_uuid` imports, delete `_service_account_kwargs`, and add this helper above `list_missions`:

```python
from .sessions import _ensure_ops_chat_service_account


async def _resolve_mission_context(
    request: Request,
    ops_session: AsyncSession,
    current_subject: str,
    org_id: UUID,
) -> tuple[dict[str, UUID], dict[str, str]]:
    """Return DB principal kwargs plus HTTP forwarding credentials.

    Today every Work-chat session is owned by the user's ops-chat service
    account on the surogates side (see ``create_live_session`` in
    ``routes/sessions.py``).  So missions created from that flow live
    under that SA.  Resolve it once so direct DB reads and mutating HTTP
    forwards use the exact same service account identity.
    """
    surogates: SurogatesClient = request.app.state.surogates
    sa_id, service_account_name, credential_name = await _ensure_ops_chat_service_account(
        surogates, org_id, ops_session, current_subject,
    )
    return (
        {"service_account_id": sa_id},
        {
            "service_account_name": service_account_name,
            "service_account_credential_name": credential_name,
        },
    )
```

- [ ] **Step 2: Replace each read endpoint's user-id resolution**

For each of `list_missions`, `get_mission`, `list_mission_tasks`, and `list_mission_workers`, replace the `user_id = await _resolve_current_ops_user_uuid(...)` lookup and the `user_id=user_id` kwarg with the principal resolver. Example for `list_missions`:

```python
@router.get("")
async def list_missions(
    request: Request,
    agent_id: str = Query(..., description="Agent id whose missions to list"),
    status_filter: str = Query(
        "active,paused",
        alias="status",
        description="Comma-separated status allowlist or 'all'",
    ),
    limit: int = Query(100, ge=1, le=200),
    current_subject: str = Depends(get_current_subject),
    ops_session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    surogates: SurogatesClient = request.app.state.surogates
    org_id = await _resolve_agent_org(agent_id, ops_session, surogates)
    principal, _forwarding = await _resolve_mission_context(
        request, ops_session, current_subject, org_id,
    )
    rows = await surogates.list_missions(
        agent_id=agent_id,
        org_id=org_id,
        status=None if status_filter == "all" else status_filter,
        limit=limit,
        **principal,
    )
    return {"missions": rows}
```

Same pattern for `get_mission`, `list_mission_tasks`, and `list_mission_workers`: call `_resolve_mission_context`, ignore the forwarding dict, and spread `**principal` into the `SurogatesClient` call.

- [ ] **Step 3: Update mutating endpoints and `_require_mission_visible`**

For `pause_mission`, `resume_mission`, and `cancel_mission`, resolve `org_id`, `principal`, and `forwarding` once, pass `principal` into `_require_mission_visible`, and pass `forwarding` into `_build_live_agent_client`. This avoids recomputing the credential name from `current_subject` and guarantees the pre-check and forwarded request use the same SA.

```python
org_id = await _resolve_agent_org(
    agent_id, ops_session, request.app.state.surogates,
)
principal, forwarding = await _resolve_mission_context(
    request, ops_session, current_subject, org_id,
)
await _require_mission_visible(
    request, mid, agent_id, org_id, principal,
)
client = await _build_live_agent_client(
    agent_id, request, ops_session, **forwarding,
)
```

Then update `_require_mission_visible` to accept the resolved `org_id` and `principal` instead of `current_subject` / `ops_session`:

```python
async def _require_mission_visible(
    request: Request,
    mission_id: UUID,
    agent_id: str,
    org_id: UUID,
    principal: dict[str, UUID],
) -> None:
    """Raise 404 unless the mission belongs to the caller's (org, principal, agent)."""
    surogates: SurogatesClient = request.app.state.surogates
    row = await surogates.get_mission(
        mission_id=mission_id,
        agent_id=agent_id,
        org_id=org_id,
        **principal,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Mission not found")
```

- [ ] **Step 4: Run ops tests**

```bash
cd /work/surogate-ops
uv run pytest tests/ -v -k mission
```

Expected: PASS (or, if no mission tests exist yet, this command is a no-op — that's fine).

- [ ] **Step 5: Manual smoke test against local cluster**

Start ops locally (`surogate-ops server`), open the Work UI in a browser, send `/mission Audit something now\n## Rubric\nrubric body` in a chat. Expected: receive a "mission created" reply, not the principal-rejection error.

- [ ] **Step 6: Commit**

```bash
git add surogate_ops/server/routes/missions.py
git commit -m "feat(missions): ops mission routes resolve SA principal for Work-chat sessions"
```

---

### Task 10: Wire shape — SDK + ops frontend types

**Files:**
- Modify: `surogates/sdk/agent-chat-react/src/types.ts:227-248`
- Modify: `surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts:135-156`
- Modify: `surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts:785-808`

- [ ] **Step 1: Update SDK `AgentChatMissionSummary` type**

Edit `/work/surogates/sdk/agent-chat-react/src/types.ts`, replace the interface:

```typescript
export interface AgentChatMissionSummary {
  id: string;
  orgId: string;
  userId: string | null;
  serviceAccountId: string | null;
  sessionId: string;
  agentId: string;
  description: string;
  rubric: string;
  status: AgentChatMissionStatus;
  iteration: number;
  maxIterations: number;
  lastEvaluationResult: string | null;
  lastEvaluationExplanation: string | null;
  lastEvaluationFeedback: string | null;
  lastEvaluationAt: string | null;
  evaluatorParseFailures: number;
  pausedReason: string | null;
  cancelledReason: string | null;
  createdAt: string;
  updatedAt: string;
}
```

- [ ] **Step 2: Rebuild + publish the SDK**

From `/work/surogates/sdk/agent-chat-react/`:

```bash
cd /work/surogates/sdk/agent-chat-react
npm run build
# bump version + publish per existing release process — confirm with
# the user before npm publish, as that is irreversible
```

Verify the new version is consumed in the ops frontend via `package.json`. If pinning by major/minor only, no change is needed; if pinned by exact version, bump it.

- [ ] **Step 3: Update ops frontend `MissionRowWire`**

Edit `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`, replace the `MissionRowWire` interface (lines ~135-156):

```typescript
interface MissionRowWire {
  id: string;
  org_id: string;
  user_id: string | null;
  service_account_id: string | null;
  session_id: string;
  agent_id: string;
  description: string;
  rubric: string;
  status: string;
  iteration: number;
  max_iterations: number;
  last_evaluation_result: string | null;
  last_evaluation_explanation: string | null;
  last_evaluation_feedback: string | null;
  last_evaluation_at: string | null;
  evaluator_parse_failures: number;
  paused_reason: string | null;
  cancelled_reason: string | null;
  created_at: string;
  updated_at: string;
}
```

And update `toAgentChatMission` (lines ~785-808):

```typescript
function toAgentChatMission(row: MissionRowWire): AgentChatMissionSummary {
  return {
    id: row.id,
    orgId: row.org_id,
    userId: row.user_id,
    serviceAccountId: row.service_account_id,
    sessionId: row.session_id,
    agentId: row.agent_id,
    description: row.description,
    rubric: row.rubric,
    status: row.status,
    iteration: row.iteration,
    maxIterations: row.max_iterations,
    lastEvaluationResult: row.last_evaluation_result,
    lastEvaluationExplanation: row.last_evaluation_explanation,
    lastEvaluationFeedback: row.last_evaluation_feedback,
    lastEvaluationAt: row.last_evaluation_at,
    evaluatorParseFailures: row.evaluator_parse_failures,
    pausedReason: row.paused_reason,
    cancelledReason: row.cancelled_reason,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}
```

- [ ] **Step 4: Run typecheck**

```bash
cd /work/surogate-ops/frontend
npm run typecheck
```

Expected: PASS. No type errors.

- [ ] **Step 5: Commit (two commits — one per repo)**

In `/work/surogates`:

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/types.ts
git commit -m "sdk(missions): nullable userId, optional serviceAccountId on Mission"
```

In `/work/surogate-ops`:

```bash
cd /work/surogate-ops
git add frontend/src/features/work/work-agent-chat-adapter.ts
git commit -m "feat(missions): accept SA-owned missions in Work chat wire types"
```

---

## Release sequencing

The fix spans two repositories with version coupling:

1. **Land surogates changes (Tasks 1-7 + 10 SDK part)** in `/work/surogates`. Cut a new wheel (`surogates-*.whl`) and a new SDK release.
2. **Bump the surogates dep in `surogate-ops/pyproject.toml`** to the new wheel URL.
3. **Land ops changes (Tasks 8-9 + 10 ops-frontend part)** in `/work/surogate-ops`.
4. **Deploy ops** — the ops migrate-on-startup runs `surogates.run_migrations`, which executes `observability.sql`, which retrofits the schema before any traffic hits the new code path.

Confirm with the user before tagging releases or pushing to PROD.

---

## Self-review notes

- **Spec coverage:** Every step in the recommendation maps to a task. Schema migration → Task 1. Loop edit → Task 5. Mission queries → Tasks 6, 8, 9. Wire types → Task 10.
- **Anonymous-channel sessions:** Still rejected — Task 5 keeps the gate but with a tightened condition (`user_id is None AND service_account_id is None`). The user message updates accordingly.
- **`/loop` is left alone:** It has the same `tenant.user_id is None` guard. Out of scope; flag as a follow-up if the user wants symmetric behavior.
- **Backfill:** No data backfill is needed. In PROD today the rejection prevented any service-account-owned missions from ever being inserted, so the missions table has only `user_id`-owned rows. The CHECK constraint is satisfied by every existing row (user_id IS NOT NULL, service_account_id IS NULL).
