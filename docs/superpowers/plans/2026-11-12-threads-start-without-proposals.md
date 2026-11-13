# Threads start without proposals: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** a project's master starts every thread itself, on the user's computer too; proposals and their cards are removed.

**Architecture:** Surogate Desktop accepts a thread's bind that names its master; the server queues that bind from the master's `agent` call and begins the thread when the computer answers. The proposal path is deleted in the harness, `web/`, ui-kit, ops and the docs.

**Tech stack:** Python (surogates), TypeScript (desktop app, `web/`, ui-kit).

**Spec:** `docs/superpowers/specs/2026-11-12-threads-start-without-proposals-design.md` (addendum to `2026-11-12-agent-family-only-design.md`).

**Branches:** on top of each repository's `feat/agent-family-only`, same worktrees.

## Global constraints

- Tests: affected files only, one worker (`-n 0`), `PYTHONDONTWRITEBYTECODE=1`; desktop with `vitest run <files>`.
- No legacy fallbacks: the card routes, events and bridge call go entirely.
- Conventional commits, no plan or task numbers in code or messages, `/usr/bin/git` with named files.

## Review Focus

1. A master on a computer project starts two threads while the computer is offline; it comes back: each thread begins once, with its goal, and the master hears each start.
2. The computer refuses a thread's bind (an old app, the folder chosen again): the master hears the thread failed, with the computer's words, and nothing begins.
3. The server names a master whose chat is not bound to a project folder on that computer, or a folder other than the project's: the app refuses and records nothing.
4. An old project log with `thread.proposed` and user-started `worker.spawned` events replays and renders without error.

---

### Task 1: Surogate Desktop binds a thread's copy the server asks for by its master

**Files:** `desktop/src/binding/binder.ts`, `desktop/src/shell/bridge.ts`, `desktop/src/shell/main.ts`, `desktop/src/testing/echo-client.ts`; tests `desktop/test/binder.test.ts`, `desktop/test/bridge.test.ts`.

- [ ] Tests first, in `binder.test.ts`:
  - a bind with `args: {folder, nonce, history: {thread: root}, master}` where `master` is bound here to a project folder records `root`'s binding to its copy (`history`), with the project's mode, and answers `{ok: {history: {thread: root}}}`; the same operation again answers the same (idempotent by `nonce`);
  - refused (`NOT_BOUND` or a binding error, nothing recorded) when: `master` has no project binding here; the folder named is not the project's; the bind names no `history`; `keepsCopies` is off (`KEEPS_NO_COPY`); the folder was replaced (`CHOOSE_AGAIN` wording).
- [ ] Implement in `Binder.admit`: when no preparation matches `nonce` and the args name a `master` and a thread copy, run `projectFolder(master)`, check the named folder is the project binding's, check copies and `noCopyAt`, then record `{root, nonce, folder, dev, ino, boot, mode, boundAt, history}`.
- [ ] Delete `prepareThreadFolder` from the binder, the bridge, `main.ts` and the echo client, with its tests.
- [ ] Run `npx vitest run test/binder.test.ts test/bridge.test.ts` and `npm run typecheck` in `desktop/`. Commit `feat(desktop): bind a project thread's copy the server asks for by its master`.

### Task 2: The server starts a thread on the user's computer

**Files:** `surogates/devices/operations.py` (`bind`, `complete`), `surogates/workstreams/threads.py` (`make_thread`, new `thread_bound`), `surogates/tools/builtin/threads.py` (`_start_thread_handler`), `surogates/tools/builtin/agents.py` (`_master_agent` answer); tests `tests/integration/test_projects_on_a_computer.py`, `tests/integration/test_master_family.py`.

- [ ] Tests first:
  - `test_a_master_starts_a_thread_on_the_projects_computer`: a computer project's master calls `agent`; a thread is made (no row, not queued) and one `bind` is queued for the project's computer with `master` and the thread's `history`; the answer says the thread waits for the computer.
  - `test_the_thread_begins_once_its_computer_binds_it`: the fake app answers the bind ok (twice, as a reconnect does): the thread gets its row, its goal and one `worker.spawned` on the master, and is queued once.
  - `test_a_refused_bind_tells_the_master_the_thread_failed`: the app answers an error: the master gets a marked `worker.failed` naming the thread and the computer's words; the thread is not begun.
- [ ] `DeviceOperations.bind(..., master: UUID | None = None)` adds `"master"` to the args.
- [ ] `make_thread(..., start: str | None = None)` keeps the goal in the thread's config (`workstream_start`) in place of `card`.
- [ ] `thread_bound(session_factory, redis, thread_id, outcome)` in `workstreams/threads.py`: on ok, begins a thread made with `workstream_start` and not begun yet (no row); on an error, tells the master the thread failed (`notify_parent_on_failure`, marked) and archives the thread. Called from `DeviceOperations.complete` when it records ("completed", not "duplicate") a bind whose args name a `master`.
- [ ] `_start_thread_handler` in a computer project makes the thread with `device`, the project's folder and `start`, and binds it with `master`, instead of refusing.
- [ ] Run the two test files plus `test_local_threads.py`, `test_device_sessions.py`. Commit `feat(projects): a master starts its threads on the user's computer`.

### Task 3: Proposals go from the harness

**Files:** `surogates/tools/builtin/agents.py` (schema `propose`/`where`, `_master_agent`), `surogates/harness/tool_schemas.py` (`describe_for_project` keeps only `resolve`), `surogates/tools/builtin/threads.py` (`_propose_threads_handler`, the computer refusal), `surogates/workstreams/threads.py` (`proposal`, `card`), `surogates/workstreams/__init__.py`, `surogates/workstreams/stream.py`, `surogates/workstreams/store.py`, `surogates/session/events.py` (`THREAD_PROPOSED`), `surogates/api/routes/workstreams.py` (the card routes, `_card`, `_card_held`, their models), `surogates/harness/loop_context_replay.py` (the user-started news), guidance `project_coordinator.md`, `project_threads_cloud.md`, `project_threads_on_computer.md`; docs `docs/sub-agents/index.md`, `docs/audit/events.md`, any projects docs; tests that propose or start from a card.

- [ ] Tests first: `propose` and `where` are in no session's `agent` schema, a master's included (`test_master_family`); the card routes answer 404 or 405; an old log's `thread.proposed` and user-started `worker.spawned` replay as nothing (`test_old_logs_replay`).
- [ ] Delete as listed; guidance: ask in chat before starting work the user did not ask for; a thread on the user's computer starts once the computer sets up its copy.
- [ ] Convert the tests that built threads from cards (`test_local_threads`, `test_desktop_local_threads`, `test_local_history_*`, `test_workstream_overview`, `test_projects_on_a_computer`, `test_workstream_threads`, `test_master_family`, `test_agent_news`, `missions/test_refinement`) to `make_thread(..., start=)` and `thread_bound`, or delete the ones about cards.
- [ ] Run the converted files and the sweep. Commit `feat(projects)!: a master starts its threads; proposals are gone`.

### Task 4: `web/` without the card flow

**Files:** `web/src/lib/local-thread.ts`, `web/src/api/workstream-routes.ts`, `web/src/features/chat/surogates-web-chat-adapter.ts`, `web/src/lib/desktop-bridge-contract.d.ts`, their script tests.

- [ ] Delete the card start flow and `prepareThreadFolder`; run `node --test` on the `scripts/web-*.test.mjs` that cover projects, and `tsc -b` against the local ui-kit build. Commit with the harness branch: `feat(web)!: threads start without cards`.

### Task 5: ui-kit without proposal cards

**Files:** `packages/agent-chat-react/src/runtime/reducer.ts`, `runtime/events.ts`, `components/chat/thread-cards.tsx`, `components/chat/simple-labels.ts`, `types.ts`; tests `thread-cards.test.tsx`, `project-threads-live.test.tsx`, `agent-chat.test.tsx`, `simple-labels.test.ts`.

- [ ] Tests first: a `thread.proposed` event draws nothing; no adapter method starts a card.
- [ ] Delete the proposal card, "Start all", the adapter's card-start methods and types, and the "Proposed a thread" label.
- [ ] Run the package's tests and `tsc`. Commit `feat(agent-chat)!: no proposal cards`.

### Task 6: ops and docs

**Files:** ops: the Copilot's synced docs (re-sync); surogate-docs: `work/desktop/projects.md`, `work/desktop/get-started.md`, `develop/reference/tool-catalog.md`, `develop/reference/background-jobs.md`, `develop/reference/event-types.md`.

- [ ] Rewrite the pages for threads the master starts (a computer thread starts once Surogate Desktop sets up its copy); build; re-sync the ops Copilot docs; commit in each repository.
