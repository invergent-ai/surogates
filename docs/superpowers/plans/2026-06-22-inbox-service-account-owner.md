# Ops "needs-you" attention indicator (replacing the ops inbox) — Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the (currently broken, always-empty) ops Studio "Inbox" with an in-place amber "needs you" indicator — a count next to the agent in the nav, and a notification glyph on the session that's waiting — so operators can see and answer blocked sessions without a separate inbox panel.

**Architecture:** The indicator is driven by the existing `inbox_items` "pending" rows (a row is `pending` while a question/approval is unanswered, and flips to `responded` when answered). Those rows are **not created for ops sessions today** because ops chats are service-account-owned (`user_id` NULL) and creation is gated on `user_id`. **Layer 1** fixes that (the rows get created for service-account sessions). **Layer 2/3** derive a per-session "needs input" flag + per-agent count from those pending rows (scoped to the operator's own `ops-chat` service account) and render the indicator; the ops Inbox nav item/panel is removed. Answering happens **inline in the chat** (the existing flow), which flips the row to `responded` and clears the indicator. Poll-based (no instant push in v1).

**Tech Stack:** Python 3.12, SQLAlchemy(async)+asyncpg, Postgres (surogates); React + the published `@invergent/agent-chat-react` SDK (web/ops). Tests via `uv run pytest` (Postgres testcontainer) and the frontends' test runners. Cross-repo: `surogates` (schema + SDK source) and `surogate-ops` (ops backend + frontend, consumes the **published** SDK).

**Spec / design:** `Misc/issues/ops-inbox-empty-service-account-sessions.md`.

## Global Constraints

- **No AI/Claude mentions** in any commit, PR, or GitHub-facing comment. No `Co-Authored-By` / "Generated with" trailers.
- **Branches:** `surogates` → `fix/inbox-service-account-owner` off `master` (created, base `692ac0c9`). `surogate-ops` → `fix/inbox-service-account-owner` off `main` (create at Task 3, off latest `main`).
- **Schema mechanism (surogates has NO Alembic):** `run_migrations` = `Base.metadata.create_all` (creates *missing tables* only) + idempotent raw DDL. A column on the existing `inbox_items` table needs BOTH the ORM model updated (fresh DBs) AND an idempotent SQL patch run on startup (existing DBs).
- **v1 minimal schema:** ONLY two column changes — `inbox_items.user_id` nullable + add nullable `service_account_id`. No CHECK constraint / no new index in v1.
- **Per-operator scoping:** the indicator reflects the operator's OWN ops-chat sessions (scope by their `ops-chat` service-account id). **No backfill.** **No live push** (poll-based).
- **Needs-you kinds:** only `input_required` and `governance_gate` light the indicator (NOT `task_complete` / `progress_checkin`).
- **SDK publishing:** `@invergent/agent-chat-react` is CI-published to npm; ops consumes the published build, the web app consumes source. So a session-row SDK change (Task 2) must be **published and the ops version bumped** before ops can render it (Task 4).
- **Rollout order:** surogates schema/creation + the SDK change land/publish **first**; the ops PR (which queries the column and uses the new SDK) follows.

---

### Task 1: surogates — inbox items can belong to a service account (schema + creation)

*(Unchanged from the prior plan — this is the shared foundation both the dot and an inbox would need.)*

**Files:**
- Modify: `surogates/db/models.py` (`InboxItem`, ~374)
- Create: `surogates/db/inbox_principal.sql`
- Modify: `surogates/db/engine.py` (`run_migrations` → `_create_all`)
- Modify: `surogates/session/store.py` (`emit_event` inbox block, ~637)
- Test: `tests/integration/test_session_store.py`

- [ ] **Step 1: Failing test** (append to `tests/integration/test_session_store.py`)

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
        session.id, EventType.INBOX_INPUT_REQUIRED,
        {"tool_call_id": "tc_1",
         "questions": [{"prompt": "Pick one", "choices": []}], "context": ""},
    )
    async with session_factory() as db:
        rows = (await db.execute(
            select(InboxItem).where(InboxItem.session_id == session.id)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].service_account_id == issued.id
    assert rows[0].user_id is None
    assert rows[0].kind == "input_required"
```
Confirm imports: `from sqlalchemy import select`, `from surogates.session.events import EventType`, `from surogates.db.models import InboxItem` (and `create_org`, `issue_service_account_token` from `.conftest`).

- [ ] **Step 2: Run, verify FAIL** — `uv run pytest tests/integration/test_session_store.py::test_inbox_item_created_for_service_account_session -v` (today the guard skips creation → 0 rows).

- [ ] **Step 3: Model** (`surogates/db/models.py`, ~374) — make `user_id` nullable, add `service_account_id`:
```python
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
```
(No CheckConstraint/Index in v1.)

- [ ] **Step 4: Idempotent patch** — create `surogates/db/inbox_principal.sql`:
```sql
-- Allow inbox items to belong to a service account instead of a user.
-- Idempotent: safe on every startup.
ALTER TABLE inbox_items ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE inbox_items
    ADD COLUMN IF NOT EXISTS service_account_id uuid REFERENCES service_accounts(id);
```

- [ ] **Step 5: Apply it** in `surogates/db/engine.py` — add `INBOX_PRINCIPAL_SQL_PATH = Path(__file__).with_name("inbox_principal.sql")` near `OBSERVABILITY_SQL_PATH`, and in `_create_all` after `apply_observability_ddl(conn)`:
```python
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(
                INBOX_PRINCIPAL_SQL_PATH.read_text(encoding="utf-8")
            )
```

- [ ] **Step 6: Creation guard** (`surogates/session/store.py`, ~637) — accept either principal, stamp `service_account_id`, keep the per-user live publish guarded:
```python
            if event_type in _INBOX_EVENTS:
                session_row = await db.get(SessionRow, session_id)
                if session_row is not None and (
                    session_row.user_id is not None
                    or session_row.service_account_id is not None
                ):
                    inbox_row = build_inbox_row(
                        event_type=event_type, event_data=redacted_data,
                        session_id=str(session_id),
                    )
                    if inbox_row is not None:
                        item = InboxItem(
                            org_id=session_row.org_id,
                            user_id=session_row.user_id,
                            service_account_id=session_row.service_account_id,
                            session_id=session_id, source_event_id=event_id,
                            kind=inbox_row.kind, title=inbox_row.title,
                            body=inbox_row.body, payload=inbox_row.payload,
                            action_ref=inbox_row.action_ref,
                        )
                        db.add(item)
                        await db.flush()
                        if session_row.user_id is not None:
                            inbox_publish = (item.id, inbox_row.kind, session_row.user_id)
```

- [ ] **Step 7: Run** — `uv run pytest tests/integration/test_session_store.py -k "inbox or list_sessions" -v` (PASS).

- [ ] **Step 8: Commit**
```bash
git add surogates/db/models.py surogates/db/inbox_principal.sql surogates/db/engine.py surogates/session/store.py tests/integration/test_session_store.py
git commit -m "feat(inbox): allow service-account-owned inbox items"
```

---

### Task 2: surogates SDK — session row can show a "needs you" glyph in its action slot

**Repo:** `surogates` (SDK source `sdk/agent-chat-react`).

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` — add optional `needsInput?: boolean | null` to `AgentChatSessionTreeNode` (and carry it through `sessionToTreeNode` if mapped there).
- Modify: `sdk/agent-chat-react/src/components/sessions/session-tree-panel.tsx` — in `TreeRow`, the action slot currently always-renders a faint delete button (`Trash2Icon`, ~line 309, `opacity-50 md:group-hover:opacity-100`). When `entry.needsInput` is true, render an **amber notification glyph** in that slot by default and reveal the delete button on hover instead (so no extra width is added and delete stays reachable).
- Test: `sdk/agent-chat-react/tests/session-tree-panel.test.tsx`.

**Interfaces:**
- Produces: `AgentChatSessionTreeNode.needsInput?: boolean | null`; the row shows the amber glyph when set. Web ignores it (its adapter never sets it).

- [ ] **Step 1:** Add `needsInput?: boolean | null` to the session-tree node type; thread it through any node-mapping (`sessionToTreeNode`) as a pass-through of the adapter-provided field.
- [ ] **Step 2: Failing test** — render `SessionTreePanel` with a node where `needsInput: true`; assert an element with the notify affordance (e.g. `aria-label="Waiting for your input"`) is present and the delete button is hidden until hover; a node without it shows the delete button as today.
- [ ] **Step 3:** Implement the conditional render in `TreeRow`'s action slot (amber glyph when `needsInput`, hover → delete). Use amber to match ops' existing attention color.
- [ ] **Step 4:** `npm test --prefix sdk/agent-chat-react -- session-tree-panel` (PASS); `npx tsc --noEmit` clean.
- [ ] **Step 5: Commit** — `git commit -m "feat(agent-chat): session row can show a needs-input indicator"`.

*(After Task 1+2 merge to surogates, the SDK is published; bump the ops dependency before Task 4.)*

---

### Task 3: surogate-ops — derive the "needs input" signal (per session + per agent)

**Repo:** `surogate-ops` (branch off latest `main`).

**Files:**
- Modify: `surogate_ops/core/surogates_client.py` — `list_agent_sessions` (~984): add a per-session `needs_input` boolean = "this session has a `pending` `inbox_items` row of kind `input_required`/`governance_gate`, for the operator's service account." Add a helper `count_attention_sessions(agent_id, org_id, service_account_id) -> int` (distinct sessions with such a pending item) for the agents list.
- Modify: `surogate_ops/server/routes/sessions.py` — the sessions-list route maps `needs_input` onto each session DTO; resolve the operator's `ops-chat` service-account id (reuse `_ensure_ops_chat_service_account`, ~292) to scope it. Add/extend the agents-list route to include each agent's attention count.
- Test: ops backend tests mirroring existing `surogates_client` DB tests.

**Interfaces:**
- Consumes (Task 1): `inbox_items` rows now exist for service-account sessions, with `status` and `service_account_id`.
- Produces: session DTO gains `needs_input: bool`; agents list gains a per-agent attention count.

- [ ] **Step 1: Failing test** — seed a service-account session with a `pending` `input_required` inbox item; assert `list_agent_sessions(... service_account_id=<sa>)` marks that session `needs_input=True` and others `False`; assert it flips to `False` once the item is `responded`. (Mirror existing ops DB-test fixtures.)
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3:** Implement the per-session pending lookup (LEFT JOIN / EXISTS against `inbox_items` filtered `status='pending'`, `kind IN ('input_required','governance_gate')`, `service_account_id = :sa`) and the agent attention count. Scope strictly by the operator's service account.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(inbox): derive per-session/agent needs-input signal for ops"`.

---

### Task 4: surogate-ops — render the indicator, remove the Inbox nav

**Repo:** `surogate-ops`. Bump `@invergent/agent-chat-react` to the version published from Task 1+2 first.

**Files:**
- Modify: the agents-list nav component (the left nav listing Copilot / Salt Agent / etc. — locate via the live "online" dot; candidates under `surogate_ops/frontend/src/features/work/`). When an agent's attention count > 0, render the count **in place of** the green "live" dot; otherwise the live dot as today.
- Modify: `surogate_ops/frontend/src/features/work/work-navbar-agent-item.tsx` (renders `<SessionTreePanel>`) and the adapter `work-agent-chat-adapter.ts` `listSessions` mapping — pass each session's `needs_input` through to `AgentChatSessionTreeNode.needsInput` so the SDK row shows the glyph.
- Remove: the "Inbox" nav entry + its route/page wiring (`work-agent-inbox-page.tsx` and the nav link to it).
- Test: ops frontend tests for the agent-count rendering and that a `needs_input` session shows the glyph.

- [ ] **Step 1:** Map `needs_input` through the ops `listSessions` adapter into `needsInput` on the tree node.
- [ ] **Step 2:** Agents-list: count-replaces-live-dot when attention > 0 (assumption: "needs attention" implies the agent is live, so hiding the live dot loses no info — confirm while wiring).
- [ ] **Step 3:** Remove the Inbox nav item + page.
- [ ] **Step 4:** Run ops frontend tests + typecheck (PASS).
- [ ] **Step 5: Commit** — `git commit -m "feat(inbox): show needs-you indicator in ops nav; remove inbox panel"`.

---

### Task 5: Verify + ship

- [ ] surogates: `uv run pytest tests/integration/test_session_store.py -k "inbox or list_sessions" -v`; SDK `session-tree-panel` test + tsc.
- [ ] ops: backend + frontend tests.
- [ ] Local manual check (VPN + `Misc/start-local.sh`): in ops Studio, trigger an `ask_user_question` in a chat → the session shows the amber glyph and the agent shows a count; answer inline → both clear on refresh. Confirm the web app is unchanged.
- [ ] `/simplify` → `/code-review`.
- [ ] PRs (technical / no-AI): **surogates** (Task 1+2) → `master` first; publish SDK; then **surogate-ops** (Task 3+4, with bumped SDK) → `main`.

---

## Self-Review

**Spec coverage:** Layer 1 schema+creation (Task 1); session-row glyph (Task 2 SDK + Task 4 wiring); per-session/agent signal (Task 3); count-replaces-live-dot + glyph-replaces-trash + remove Inbox nav (Task 4); per-operator scoping (Task 3 scopes by the operator's service account); needs-you kinds only (Task 3 filter); no backfill / poll-based (no code recovers old events or pushes live); reuse of the pending→responded lifecycle (answering inline clears it — no respond plumbing built).

**Placeholder scan:** Tasks 3/4 reference "mirror existing ops tests" and "locate the agents-list nav component via the live dot" — the ops test harness and the exact agents-list file weren't pinned at plan time; the implementer resolves them against the current ops tree. Task 1 (and the SDK row slot at session-tree-panel.tsx ~309) are concrete.

**Type consistency:** `needs_input` (backend/DTO, snake) ↔ `needsInput` (SDK `AgentChatSessionTreeNode`, camel) is the one mapping boundary, mapped in the ops adapter (Task 4 Step 1). `service_account_id: UUID` is the scoping param in Task 3, matching `InboxItem.service_account_id` from Task 1.

**Cross-repo sequencing:** SDK change (Task 2) is published from the surogates PR before ops (Task 4) bumps and uses it — captured in Global Constraints + Task 4 header.
