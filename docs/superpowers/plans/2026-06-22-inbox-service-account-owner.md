# Fix the ops inbox (service-account-owned sessions) — Implementation Plan / Record

> Implemented on branch `fix/inbox-service-account-owner` (surogates + surogate-ops),
> verified live in Studio. Steps marked `[x]` are done. Remaining: `/simplify`,
> `/code-review`, PRs.

**Goal:** Make the (currently broken, always-empty) ops Studio **Inbox** work, like the
agent-chat inbox — it surfaces items needing input and doubles as a history of interactions.

**Decision:** Keep the ops Inbox (boss's call, 2026-06-23). This supersedes the earlier
"needs-you indicator instead of an inbox" pivot (dropped).

**Architecture:** The ops Inbox panel and unread badge already exist in the ops frontend;
they were empty only because the backend returned nothing. Two backend changes fix it:
1. **Layer 1 (surogates):** create `inbox_items` for service-account sessions (today gated on
   `user_id`, which ops sessions lack), and publish live events keyed by the session principal.
2. **Layer 2 (surogate-ops):** scope the existing inbox query / item lookups / SSE stream by
   the operator's `ops-chat` service account instead of `user_id`.
No SDK change and no ops UI change are needed — once the rows exist and are scoped, the
existing panel + `useInboxUnreadCount` badge populate and update live on their own.

**Tech Stack:** Python 3.12, SQLAlchemy(async)+asyncpg, Postgres (surogates); React + the
published `@invergent/agent-chat-react` SDK (ops consumes the published build). Tests via
`uv run pytest`. Cross-repo: `surogates` (schema + runtime) and `surogate-ops` (backend).

**Spec / design:** `Misc/issues/ops-inbox-empty-service-account-sessions.md`.

## Global Constraints

- **No AI/Claude mentions** in any commit, PR, or GitHub comment. No trailers.
- **Branches:** both repos on `fix/inbox-service-account-owner` (surogates off `master`,
  surogate-ops off `main`).
- **Schema mechanism (surogates has NO Alembic):** `run_migrations` = `create_all` (missing
  tables only) + idempotent raw DDL. A column change on the existing `inbox_items` table needs
  BOTH the ORM model (fresh DBs) AND an idempotent SQL patch run on startup (existing DBs).
- **Per-operator scoping:** the inbox shows the operator's OWN ops-chat sessions' items
  (scope by their `ops-chat` service-account id; the legacy stored-token SA is included too).
  No backfill.
- **Migration + release order:** surogates schema/creation lands and **a surogates release is
  cut** first (ops pins the *published* surogates wheel, not the checkout); ops then bumps the
  surogates dependency and its PR (which queries the new column) follows. Migration-first.

---

### Task 1: surogates — inbox items can belong to a service account  ✅

**Files:** `surogates/db/models.py`, `surogates/db/inbox_principal.sql` (new),
`surogates/db/engine.py`, `surogates/session/store.py`, `tests/integration/test_session_store.py`.

- [x] **Model** — `InboxItem.user_id` nullable; add `service_account_id` (FK
  `service_accounts.id`, nullable); add `CheckConstraint(... = 1, name="ck_inbox_items_one_principal")`
  enforcing exactly one principal.
- [x] **Idempotent DDL** — `db/inbox_principal.sql`: `ALTER COLUMN user_id DROP NOT NULL`,
  `ADD COLUMN IF NOT EXISTS service_account_id ...`, and the constraint inside a
  `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL $$`. Applied from `engine.py`
  (`INBOX_PRINCIPAL_SQL_PATH`, run via the extracted `_execute_sql_script` after the
  observability DDL in `_create_all`).
- [x] **Creation guard** (`store.py`) — accept either principal; stamp `service_account_id`.
- [x] **Live publish** (`store.py`) — publish to `surogates:inbox:{principal}` where
  `principal = session_row.user_id or session_row.service_account_id`. User sessions keep
  publishing to the same user channel (agent chat unaffected); SA sessions now publish too.
- [x] **Tests** — `test_inbox_item_created_for_service_account_session` (SA session → item with
  `service_account_id`, `user_id` NULL) and `test_inbox_item_requires_exactly_one_principal`
  (raw INSERT with both/neither principal → IntegrityError). 2/2 pass; agent-chat inbox path
  untouched (19/19 surogates inbox tests pass).

---

### Task 1.5: surogates — same judge-rescue for service-account sessions  ✅

So ops behaves exactly like agent chat, not just for explicit tool calls.

**Files:** `surogates/harness/loop.py`, `tests/test_harness_resilience.py`.

- [x] **Guard** — `_maybe_route_final_response_to_inbox` gated the final-response judge rescue
  on `if session.user_id is None: return None`, so plain-text "needs you" answers (and
  plain-text questions the agent didn't route through `ask_user_question`) never became inbox
  items for ops. Changed to `if session.user_id is None and session.service_account_id is None:
  return None`. Sub-agent (`parent_id`) and `scheduled` sessions stay excluded. The only live
  call site is the main loop; the other (`_maybe_convert_final_response_to_ask_user_question`)
  is dead code.
- [x] **Test** — TDD: a service-account session with a plain-text "sign in" final answer now
  routes to `action_required` (failed before the guard change, passes after); existing
  user-session rescue tests still pass.
- **Cost note:** the judge LLM call now also runs on every final plain-text response for ops
  sessions — accepted to match agent-chat behaviour.

### Task 2: surogate-ops — scope the inbox by the operator's service account  ✅

**Files:** `surogate_ops/core/surogates_client.py`, `surogate_ops/server/routes/sessions.py`.

- [x] **Client** — the inbox methods take `service_account_ids: list[UUID]` (was `user_id`)
  and filter `InboxItem.service_account_id.in_(service_account_ids)` (in `list_inbox` and the
  scoped-item lookup). Imports `InboxItem` from `surogates.db.models`.
- [x] **Routes** — `_resolve_inbox_context` resolves the operator's `ops-chat-{org}-{operator}`
  service account (`_ensure_ops_chat_service_account`) plus the legacy stored
  `surogates_api_token` SA, returning `(surogates, org_id, service_account_ids, user_uuid,
  user_id)`. All six inbox routes (list / get / mark-read / respond-governance / respond-action
  / stream) scope by `service_account_ids`.
- [x] **Live stream** — the inbox SSE route subscribes to `surogates:inbox:{sa}` for each
  operator service-account id (was the user channel) so new items push live.
- [x] **Tests** — 72 ops inbox/session tests pass. (They mock the client, so the editable-vs-
  copied-surogates runtime mismatch only surfaced in Studio, not in tests — see the release
  note.)

---

### Task 2.5: acknowledge-only items persist until read/acknowledged  ✅

Stops informational items vanishing on the expiry timer; "read it" dismisses it.

**Files:** `surogates/jobs/inbox_expire.py`, `tests/integration/test_inbox_expire.py`;
`surogate-ops/frontend/.../work-agent-inbox-page.tsx`.

- [x] **surogates** — the expiry sweep excludes `task_complete` / `progress_checkin`
  (`_ACKNOWLEDGE_ONLY_KINDS`): they have nothing to act on against a live session, so a
  terminal session no longer auto-expires them. They persist until read or acknowledged.
  TDD: a terminal session expires its `input_required` but keeps its `task_complete`.
- [x] **ops** — clicking a notification only **previews** it (it stays in Active). **Opening
  the session** (the chat page, via any entry point — the inbox, the sessions list, a deep
  link) **expires** that session's pending `task_complete` / `progress_checkin` items
  (SA-scoped soft-delete → `expired`), so they clear from Active and, since expired items
  aren't listed, aren't kept in History either. The dismissal lives on the chat page
  (`work-agent-chat-page.tsx`, `listInbox({session_id})` + `deleteInboxItem`), not the inbox
  button, so every way of opening the session clears it. The Acknowledge button still files to
  History (`acknowledged`). Items needing a response (input_required / governance_gate /
  action_required) are unaffected.
- Skipped a frontend unit test (no existing test harness for this page; helper is a pure,
  tsc-checked display filter) — verified live instead.

### Task 2.6: don't notify for a session you're actively watching  ✅

The cleanest form of the acknowledge-only UX: rather than create the notification
and dismiss it, **don't create it** if the operator is already watching the session.

- [x] **surogates** — in `store.emit_event`, for `task_complete` / `progress_checkin`
  (`_PRESENCE_SUPPRESSED_KINDS`) only, skip creating the item when the session has a **live
  viewer**. Presence reuses the existing session-event SSE: viewers `SUBSCRIBE` to
  `surogates:session:{id}`, so `PUBSUB NUMSUB > 0` means someone is watching
  (`_session_has_live_viewer`, best-effort — defaults to "create" on any Redis error so a
  notification is never dropped). Ops viewers count too: ops proxies the chat stream to the
  same surogates `/v1/api/sessions/{id}/events` endpoint. NUMSUB counts exact subscribers
  only, so the pattern subscribers used elsewhere don't create false positives.
  Kinds that need a response (input_required / governance / action) are always created. TDD:
  with a live viewer, a `task_complete` is suppressed while an `input_required` is still made.
- [x] **ops** — removed the chat-page "dismiss on item-stream event" effect (it was the
  while-viewing case, now handled at creation). Kept the on-open dismissal for items created
  while you were away. Net model: **watching → never notified; away → notified, persists
  until you open the session or acknowledge.**

### Task 3: Verify + ship

- [x] surogates inbox store tests (2/2) + ops inbox/session tests (72/72) green.
- [x] Local manual check (VPN + `Misc/start-local.sh`): Brand Designer Inbox populates (pending
  question + history), unread badge shows the count, and the badge updates **live** when a new
  `ask_user_question` fires (no refresh). Agent-chat side unchanged.
- [ ] `/simplify` → `/code-review`.
- [ ] PRs (technical / no-AI): **surogates** → `master` first, **cut a release**; bump the ops
  `surogates` dependency to it; then **surogate-ops** → `main`.

---

## Notes / gotchas surfaced during implementation

- **ops venv pins the published surogates wheel**, not the checkout — so the ops server did not
  see Layer 1's new column until `surogates` was reinstalled editable into the ops venv
  (`uv pip install -e ../surogates`). For deploy this means: surogates release **then** ops dep
  bump. `Misc/start-local.sh` does not `uv sync`, so the editable override survives restarts.
- The **unread badge is unread-only** (`useInboxUnreadCount` counts pending items with
  `read_at IS NULL`, excluding expired). An empty badge can simply mean everything is read.
- **No SDK / no nav change** — unlike the dropped dot plan. The ops Inbox panel and badge were
  already wired; this is a pure backend scoping + live-publish fix.
