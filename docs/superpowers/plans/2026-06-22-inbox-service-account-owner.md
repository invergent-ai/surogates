# Inbox supports service-account-owned sessions — Implementation Plan (v1, minimal)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make inbox items get created for (and be readable in ops for) service-account-owned sessions, so the ops Studio inbox stops being permanently empty.

**Architecture:** Today the inbox is per-user: `inbox_items.user_id` is NOT NULL and `store.emit_event` only creates an item when `session.user_id` is set. Ops chats run under a per-operator service account (`ops-chat-{org}-{operator}`), so those sessions have `user_id = NULL` and never produce inbox items. We let `inbox_items` belong to **either** a user or a service account (mirroring how `sessions` already models its principal), stamp whichever the session has, and change the ops inbox queries to scope by the operator's service-account id.

**Tech Stack:** Python 3.12, SQLAlchemy (async) + asyncpg, Postgres. Tests via `uv run pytest` against a throwaway Postgres testcontainer. Cross-repo: `surogates` (runtime + schema) and `surogate-ops` (ops backend).

**Spec / design:** `Misc/issues/ops-inbox-empty-service-account-sessions.md` (root cause, live DB proof, resolved decisions).

## Global Constraints

- **No AI/Claude mentions** in any commit message, PR title/body, or code comment that lands on GitHub. No `Co-Authored-By` / "Generated with" trailers.
- **Branches:** `surogates` → `fix/inbox-service-account-owner` off `master` (already created, base `692ac0c9`). `surogate-ops` → `fix/inbox-service-account-owner` off `main` (create at Task 2, off the latest `main`).
- **Schema mechanism (surogates has NO Alembic):** `run_migrations` runs `Base.metadata.create_all` (creates *missing tables* only — does NOT alter existing tables) + idempotent raw DDL. A column added to the existing `inbox_items` table needs BOTH: (1) the ORM model updated (fresh DBs via `create_all`), and (2) an idempotent SQL patch run on startup (existing dev/prod DBs).
- **v1 = minimal schema:** ONLY two column changes — make `user_id` nullable and add a nullable `service_account_id`. **No** exactly-one-principal CHECK constraint and **no** new index in v1 (the table is small; both are deferrable polish).
- **Scope (locked):** per-operator scoping (ops queries by the operator's `ops-chat` service-account id); **no backfill** of already-orphaned events; **no live badge/SSE for service-account items** in v1 — items appear when the inbox is opened/refreshed (the per-user live publish is left untouched, so it simply doesn't fire for service-account items).
- **Rollout ordering:** the surogates PR (schema + creation) must merge/deploy **before** the surogate-ops PR (which queries the new column).
- **Tests:** `uv run pytest <path> -v`. Store/integration tests use the Postgres testcontainer wired in `tests/integration/conftest.py`. Service-account session helper: `issue_service_account_token(session_factory, org_id)` → object with `.id`; `create_session(user_id=None, service_account_id=<id>, channel="api", ...)`.

---

### Task 1: surogates — inbox items can belong to a service account (schema + creation)

**Files:**
- Modify: `surogates/db/models.py` (`InboxItem`, ~lines 374-376)
- Create: `surogates/db/inbox_principal.sql` (idempotent patch for existing DBs)
- Modify: `surogates/db/engine.py` (`run_migrations` → `_create_all`)
- Modify: `surogates/session/store.py` (`emit_event` inbox block, ~lines 635-660)
- Test: `tests/integration/test_session_store.py`

**Interfaces:**
- Produces: `InboxItem.service_account_id: uuid.UUID | None` (nullable); `InboxItem.user_id` becomes nullable. `store.emit_event` creates an inbox item when the session has *either* principal, stamping both `user_id` and `service_account_id` from the session.

- [ ] **Step 1: Write the failing test** (append to `tests/integration/test_session_store.py`)

```python
async def test_inbox_item_created_for_service_account_session(
    session_store, session_factory,
):
    """A service-account-owned session (user_id NULL) still produces an inbox
    item, stamped with service_account_id instead of user_id."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    session = await session_store.create_session(
        user_id=None, org_id=org_id, agent_id="test-agent",
        service_account_id=issued.id, channel="api",
    )
    await session_store.emit_event(
        session.id,
        EventType.INBOX_INPUT_REQUIRED,
        {"tool_call_id": "tc_1",
         "questions": [{"prompt": "Pick one", "choices": []}],
         "context": ""},
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].service_account_id == issued.id
    assert rows[0].user_id is None
    assert rows[0].kind == "input_required"
```

Confirm these imports exist at the top of the test module (add any missing):
```python
from sqlalchemy import select
from surogates.session.events import EventType
from surogates.db.models import InboxItem
# create_org, issue_service_account_token already imported from .conftest
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/integration/test_session_store.py::test_inbox_item_created_for_service_account_session -v`
Expected: FAIL — `emit_event` skips inbox creation when `user_id is None`, so `len(rows) == 0`. (The model change in Step 3 also makes the fresh testcontainer schema have the column.)

- [ ] **Step 3: Update the `InboxItem` model** (`surogates/db/models.py`, ~374)

Make `user_id` nullable and add `service_account_id` immediately after it:
```python
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
```
(Do NOT add a CheckConstraint or new Index in v1. `Optional`, `ForeignKey`, `UUID` are already imported.)

- [ ] **Step 4: Create the idempotent patch for existing DBs** (`surogates/db/inbox_principal.sql`)

```sql
-- Allow inbox items to belong to a service account instead of a user.
-- Idempotent: safe to run on every startup (no-ops once applied).
ALTER TABLE inbox_items ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE inbox_items
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id);
```

- [ ] **Step 5: Apply the patch in `run_migrations`** (`surogates/db/engine.py`)

Near the top, beside `OBSERVABILITY_SQL_PATH = Path(__file__).with_name("observability.sql")`:
```python
INBOX_PRINCIPAL_SQL_PATH = Path(__file__).with_name("inbox_principal.sql")
```
In `_create_all` (inside `run_migrations`), after the `apply_observability_ddl(conn)` line, add the patch run (same raw-connection pattern `apply_observability_ddl` uses for multi-statement scripts):
```python
            await conn.run_sync(Base.metadata.create_all)
            await apply_observability_ddl(conn)
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(
                INBOX_PRINCIPAL_SQL_PATH.read_text(encoding="utf-8")
            )
```

- [ ] **Step 6: Update the inbox-creation block** (`surogates/session/store.py`, `emit_event`, ~637)

Accept either principal and stamp `service_account_id`; keep the per-user live publish guarded so it only fires when there is a user (service-account live notifications are out of v1 scope):
```python
            if event_type in _INBOX_EVENTS:
                session_row = await db.get(SessionRow, session_id)
                if session_row is not None and (
                    session_row.user_id is not None
                    or session_row.service_account_id is not None
                ):
                    inbox_row = build_inbox_row(
                        event_type=event_type,
                        event_data=redacted_data,
                        session_id=str(session_id),
                    )
                    if inbox_row is not None:
                        item = InboxItem(
                            org_id=session_row.org_id,
                            user_id=session_row.user_id,
                            service_account_id=session_row.service_account_id,
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
                        if session_row.user_id is not None:
                            inbox_publish = (
                                item.id,
                                inbox_row.kind,
                                session_row.user_id,
                            )
```
(The existing `inbox_publish` consumer after commit is unchanged; for service-account items `inbox_publish` stays `None`, so no live publish is attempted — matching the v1 "no live badge for SA items" decision.)

- [ ] **Step 7: Run the new test + the existing inbox/list_sessions tests, verify pass**

Run: `uv run pytest tests/integration/test_session_store.py -k "inbox or list_sessions" -v`
Expected: PASS (new service-account test passes; existing user-owned behaviour unchanged).

- [ ] **Step 8: Commit**

```bash
git add surogates/db/models.py surogates/db/inbox_principal.sql surogates/db/engine.py surogates/session/store.py tests/integration/test_session_store.py
git commit -m "feat(inbox): allow service-account-owned inbox items"
```

---

### Task 2: surogate-ops — scope inbox queries by service account

**Repo:** `surogate-ops`. First: `git fetch origin && git checkout main && git pull && git checkout -b fix/inbox-service-account-owner`.

**Files:**
- Modify: `surogate_ops/core/surogates_client.py` — the inbox query/lookup methods: `list_inbox` (~1770), `get_inbox_item`, `mark_inbox_read`, `mark_inbox_responded`, `_get_scoped_inbox_item`.
- Test: ops backend test for `surogates_client` inbox methods (mirror the existing `surogate_ops` DB-test pattern / fixtures used by other `surogates_client` tests; add a focused test if none covers inbox).

**Interfaces:**
- Produces: each inbox method accepts `service_account_id: UUID` and filters `InboxItem.service_account_id == service_account_id` instead of `InboxItem.user_id == user_id`, keeping the existing `org_id` + `agent_id` (+ session-join) filters.
- Consumes (Task 1): inbox rows now carry `service_account_id`.

- [ ] **Step 1: Write the failing test** — insert an inbox item on a service-account session in the test DB, then assert `list_inbox(service_account_id=<sa>, agent_id=..., org_id=...)` returns it, and a different `service_account_id` returns nothing. Follow the same testcontainer/`session_factory` fixture the other `surogates_client` tests use.

- [ ] **Step 2: Run, verify it fails** — `list_inbox` filters by `user_id` today, so a service-account id returns nothing. Expected: FAIL.

- [ ] **Step 3: Swap the principal predicate** in `_get_scoped_inbox_item` and the four public methods. Replace `InboxItem.user_id == user_id` with `InboxItem.service_account_id == service_account_id` and rename the parameter. Example (`list_inbox`):

```python
    async def list_inbox(
        self,
        *,
        agent_id: str,
        org_id: UUID,
        service_account_id: UUID,   # was: user_id: UUID
        status: str | None = None,
        kind: str | None = None,
        session_id: UUID | None = None,
        cursor: tuple[datetime, int] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        filters = [
            InboxItem.org_id == org_id,
            InboxItem.service_account_id == service_account_id,   # was user_id
            SessionRow.org_id == org_id,
            SessionRow.agent_id == agent_id,
        ]
        ...
```
Apply the same predicate swap to `get_inbox_item`, `mark_inbox_read`, `mark_inbox_responded`, and `_get_scoped_inbox_item`.

- [ ] **Step 4: Run, verify pass** — the service-account item is returned; a different service account returns nothing.

- [ ] **Step 5: Commit**

```bash
git add surogate_ops/core/surogates_client.py <test file>
git commit -m "feat(inbox): scope ops inbox queries by service account"
```

---

### Task 3: surogate-ops — inbox routes resolve the operator's ops-chat service account

**Repo:** `surogate-ops`.

**Files:**
- Modify: `surogate_ops/server/routes/sessions.py` — `_resolve_inbox_context` (~874) and the inbox route call sites that pass the principal to the `surogates.*` inbox methods (`list_inbox` ~894, `stream_inbox`, `get_inbox_item`, `mark_inbox_item_read`, respond, ack).
- Reuse: `_ensure_ops_chat_service_account(...) -> (service_account_id, name, credential)` (~292) — already used by the chat-session route.
- Test: ops route test for `GET /inbox` returning the operator's service-account items.

**Interfaces:**
- Consumes (Task 2): the `surogates_client` inbox methods now take `service_account_id`.

- [ ] **Step 1: Write the failing test** — call `GET /inbox` as an operator whose ops-chat session raised an inbox item; assert the item is returned. Expected initially: FAIL (route passes `user_id`).

- [ ] **Step 2: Resolve the service-account id in `_resolve_inbox_context`** (reuse `_ensure_ops_chat_service_account`, exactly as the chat route does), and have it return/expose `service_account_id`. Update each inbox route to pass `service_account_id=...` to the matching `surogates.*` call instead of `user_id=user_uuid`:

```python
async def _resolve_inbox_context(...):
    ...
    service_account_id, _name, _cred = await _ensure_ops_chat_service_account(
        ops_session, current_subject, org_id,
    )
    return ...  # include service_account_id alongside org_id/agent_id
```

- [ ] **Step 3: Run, verify pass** — the operator sees their own ops-chat inbox items; a different operator (different service account) does not.

- [ ] **Step 4: Commit**

```bash
git add surogate_ops/server/routes/sessions.py <test file>
git commit -m "feat(inbox): ops inbox routes resolve operator service account"
```

---

### Task 4: Verify end-to-end + local check

- [ ] **Step 1:** surogates — `uv run pytest tests/integration/test_session_store.py -k "inbox or list_sessions" -v` (green).
- [ ] **Step 2:** ops backend inbox tests (green).
- [ ] **Step 3:** Local manual check (VPN + `Misc/start-local.sh`): chat with an agent in ops Studio, trigger an `ask_user_question`, confirm it now appears in the ops inbox on open/refresh. (Note: local points at the shared dev DB, so the patch SQL adds the `service_account_id` column there — additive/safe.)
- [ ] **Step 4:** Confirm the agent-chat (web) inbox is unchanged (still shows user-owned items; service-account items absent there).

---

### Then: /simplify → /code-review → two PRs

- `/simplify` and `/code-review` on the changes.
- Two PRs, technical / no-AI-mention: **surogates** (schema + creation) → `master` **merges first**, then **surogate-ops** (query + routes) → `main`.

---

## Self-Review

**Spec coverage:** schema minimal (Task 1 model + 2-statement patch SQL), creation for service-account sessions (Task 1 store), ops query by service account (Task 2), ops route resolution of the operator's service account (Task 3), per-operator scoping (the `ops-chat` SA is per-operator), no backfill (nothing recovers old events), no live badge (Task 1 Step 6 leaves `inbox_publish` None for service-account items). Rollout order in Global Constraints.

**Placeholder scan:** ops test fixtures (Tasks 2/3 Step 1) say "mirror existing ops DB/route tests" rather than exact fixture code — the ops test harness wasn't explored at plan time; the implementer follows existing `surogate_ops` test patterns. All surogates code is concrete.

**Type consistency:** `service_account_id: UUID` is the parameter name across all five `surogates_client` methods and the route call sites; `InboxItem.service_account_id` matches the model field added in Task 1; `session_row.service_account_id` already exists on `SessionRow`.
