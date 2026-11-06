# New chats in their own folder, and chats taking turns on a shared folder: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new chat in Surogate Desktop works in its own new folder by default; the user can still pick the last folder or any folder; the sheet warns when the folder is in use; two chats on one folder take turns by whole answers, the second waiting (and saying so) instead of failing.

**Architecture:** The page offers `new | last | pick` (feature-detected on the bridge); the binder makes and offers a new folder for `new`, and the sheet warns from the tool hosts' own record of which chat holds a folder. The wait happens in `ToolHosts` before a chat's host is spawned: a per-folder FIFO of chats waits until no other chat's host is live on that folder (dev, ino), for at most 10 minutes. A chat's host lets go at its turn's end, heard through a per-host follow of the chat's event stream. The desktop tells the server with a new link frame `op_waiting`, sent only to a server whose welcome says `waits`; the worker turns it into `device.waiting` with reason `folder`, which the chat SDK (ui-kit repo) renders.

**Tech Stack:** TypeScript (Electron main, vitest), React 19 + `@invergent/ui-sdk` + lucide-react (web, `node --test` scripts), Python 3.12 (FastAPI, Redis, pytest with testcontainers), `@invergent/agent-chat-react` in the ui-kit repo.

**Spec:** `docs/superpowers/specs/2026-11-06-new-chat-own-folder-design.md` (read it whole; its "Builder's call" notes are decisions, not suggestions).

## Todo

- [ ] Task 1: The binder offers a new folder for `new`, and the bridge accepts it
- [ ] Task 2: The tool hosts know which chat's host is on a folder
- [ ] Task 3: The sheet warns about a folder in use, with New folder and Browse…
- [ ] Task 4: A chat's host lets its folder go at its turn's end
- [ ] Task 5: The app follows each chat host's turns
- [ ] Task 6: A chat waits for another chat's folder, up to 10 minutes
- [ ] Task 7: Several waiting chats get the folder in the order they asked
- [ ] Task 8: The desktop tells the server an operation waits for the folder
- [ ] Task 9: The server's link takes `op_waiting`
- [ ] Task 10: The worker says `device.waiting` with reason `folder`
- [ ] Task 11: The chat SDK shows the folder wait (ui-kit repo)
- [ ] Task 12: The page's folder choice, and older desktops
- [ ] Task 13: The folder menu under the composer
- [ ] Task 14: The web takes the SDK that shows the folder wait

## Global Constraints

- Repo `/home/monica/invergent/surogates`, branch `fix/desktop-new-chat-folder` (already checked out; stay on it). Task 11 alone is in `/home/monica/invergent/ui-kit` (see that task).
- Update the Todo checkboxes above in the same commit as each task.
- Commit titles: Conventional Commits `type(scope): subject` (scopes used here: `desktop`, `web`, `devices`, `workstreams`). Never mention Claude or AI; no `Co-Authored-By` or any trailer. Check `git log -1 --format=%B` after each commit.
- Never reference plan, task or step numbers (or private bug ids) in code comments, commit messages, test names or any committed artifact.
- Production code only: no TODOs, stubs or placeholders. TDD: the failing test first, run and seen failing for the right reason, then the code.
- Stage files explicitly by path. Never `git add -A` / `git add .`. Never `git stash`, never `git checkout` over uncommitted work.
- No em or en dashes in any user-facing copy (desktop sheet, error messages the agent passes on, web, SDK). The UI copy below is verbatim from the spec; use it exactly.
- Comments: the codebase's own style, plain terse prose sentences saying what a thing is or why; no banners, no "NOTE:", no restating code.
- Node 22: prefix every npm/node command with `PATH=$HOME/.local/node/bin:$PATH`.
- Desktop tests: `cd desktop && npm test -- <file>` (pretest builds; `[slow]` tests via `npm run test:slow -- <file>`). Web tests: `cd web && npm run test:<name>`; typecheck `npm run typecheck`; copy check `npm run test:copy`. Python: `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" <paths>` from the repo root (integration tests use Docker test containers).
- Values fixed by the spec: folder wait limit `10 * 60_000` ms; `IDLE_MS` (120 s) stays as the fallback; `LOCK_WAIT_MS` (10 s) stays for holders that are not chats and for unknown holders; `newFolder` naming rules unchanged.

Exact copy:

| Where | Text |
|---|---|
| Sheet warning (busy folder) | `Another chat on this computer is working in this folder. The two chats will take turns: this chat waits while the other one is answering.` |
| Sheet buttons, busy | `Cancel` · `New folder` · `Browse…` · `Use this folder` |
| Sheet buttons, not busy | `Cancel` · `Change…` · `Use this folder` (unchanged) |
| Wait limit error (type `busy`) | `Another chat on this computer has been working in this folder for more than 10 minutes, so this chat stopped waiting. Try again once it is done, or start a new chat in a new folder.` |
| Line under the composer | `Works in` [menu button] `·` `You confirm the folder after your first message.` |
| Menu `new` | label `A new folder`, hint `Made for this chat`, button `a new folder`, icon `FolderPlusIcon` |
| Menu `last` | label `The last folder`, hint `Where your last chat worked`, button `the last folder`, icon `HistoryIcon` |
| Menu `pick` | label `Choose a folder…`, hint `The folder window opens after you send`, button `a folder you choose`, icon `FolderOpenIcon` |

Copy the spec does not fix, decided here (confirm with Monica before Task 11 merges):

| Where | Text |
|---|---|
| Chat notice, reason `folder` (SDK) | `Waiting for another chat working in the same folder. This chat goes on once that one is done.` |
| Sidebar status line, reason `folder` | `Waiting for another chat in the same folder` |

## Review Focus

- A quit while a chat waits for the folder: the wait ends at once with the quitting answer, nothing is left queued, and no host is spawned afterwards. (Task 6 test.)
- A chat whose own earlier host is still on the folder (its coordinator's folder chosen again, or its old host still exiting) never waits on itself. (Task 6 test.)
- Project threads (hosts on copies), landings and put-backs never count as a chat holding the folder and never queue; a chat behind a landing keeps today's short wait and message. (Task 6 test.)
- The link drops and comes back while an operation still waits: the waiting notice is sent again after the new welcome, and nothing is sent to a server whose welcome lacks `waits`. (Task 8 test.)
- A waiter that is cancelled while it waits (the chat stopped, a file-panel read timed out) leaves the queue, and the chats behind it move up. (Task 7 test.)

---

### Task 1: The binder offers a new folder for `new`, and the bridge accepts it

**Files:**
- Modify: `desktop/src/binding/binder.ts:222-246` (`prepareFolder`), `desktop/src/shell/bridge.ts:20,100-104`, `desktop/src/shell/main.ts:1579-1580` (types only), `desktop/src/shell/preload.cts:125-131`, `web/src/lib/desktop-bridge-contract.d.ts:1-5,106-119`
- Test: `desktop/test/binder.test.ts`, `desktop/test/bridge.test.ts` (line 164 asserts `"new"` is refused today: change that row to another word, `"other"`)

**Interfaces:**
- Produces: `export type DesktopFolderChoice = "new" | "last" | "pick";` in the contract; `DesktopBridge.prepareFolder(choice: DesktopFolderChoice, thread?)`; `readonly offersNewFolder?: true` on `DesktopBridge` (a desktop that has it takes `"new"`; the comment says desktops built before it have none and the page looks first). `Binder.prepareFolder(choice: "new" | "last" | "pick", window, signal, thread?)`. The preload exposes `offersNewFolder: true`.

- [ ] **Step 1: Write the failing tests** in `binder.test.ts` under "preparing a new chat's folder":
  - `it("offers a new folder of its own for a new chat even when a last folder is bound")`: bind `notes` for `OTHER`; `prepareFolder("new", ...)` with `User([], [{ mode: "free" }])`; expect the sheet folder to match `~/Surogate/Research-assistant/YYYY-MM-DD`, `user.dialogs` empty, result folder equal to it.
  - `it("removes the new folder made for new when the user cancels")`: `User([], [null])`; result null; `existsSync(made)` false.
  - `it("offers a new folder for last when the last one can no longer be used")` exists in spirit ("shows the last folder bound ... once that one has gone"): keep it passing unchanged as the guard that `"last"` behaves as today.
  - In `bridge.test.ts`: `prepareFolder` with `"new"` reaches `calls.prepareFolder("new", "7", null)`; the refused row becomes `["prepareFolder", ["other"], "Not a folder choice"]`.
- [ ] **Step 2: Run** `npm test -- test/binder.test.ts test/bridge.test.ts`. Expected: the new cases FAIL (`"new"` makes no folder; bridge throws "Not a folder choice").
- [ ] **Step 3: Implement.** In `prepareFolder`, `made` is a new folder when `choice === "new"`, or when `choice === "last"` and no last folder is usable; `"pick"` makes none. Fix the doc comment to say so. Bridge accepts the three words. Contract: the type, the optional flag, and replace "Version 1 stays open to new calls until the first desktop release" with a line saying desktops are released, so what came after the first ones is looked for (as the file already says for `getBinding`).
- [ ] **Step 4: Run** the same tests, then `cd web && npm run typecheck`. Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add desktop/src/binding/binder.ts desktop/src/shell/bridge.ts desktop/src/shell/main.ts desktop/src/shell/preload.cts web/src/lib/desktop-bridge-contract.d.ts desktop/test/binder.test.ts desktop/test/bridge.test.ts docs/superpowers/plans/2026-11-06-new-chat-own-folder.md
git commit -m "feat(desktop): offer a new chat a new folder of its own"
```

### Task 2: The tool hosts know which chat's host is on a folder

**Files:**
- Modify: `desktop/src/hosts/tool-hosts.ts` (`hostOf` ~623-653, a new private `chatHostFor`, `keep` ~838), `desktop/src/vm/executor.ts`, `desktop/src/browser/executor.ts` (~392, beside `keepsCopies`), `desktop/src/shell/device-stack.ts:31-67` (`ToolLayer`)
- Test: `desktop/test/tool-hosts.test.ts` (the FakeHost describe block)

**Interfaces:**
- Produces: `ToolHosts.folderInUse(dev: number, ino: number): boolean` (true while a live host of a chat, not a thread's copy, landing or put-back, is on that folder by dev and ino, starting, working or in its idle tail). `VmExecutor.folderInUse` and `Browsing.folderInUse` delegate; `ToolLayer.folderInUse?(dev: number, ino: number): boolean`. Private, used by Tasks 4, 6, 7: `private readonly chats = new Map<Host, string>()` (each chat's host by its root, until it exits); `private chatOn(folder: { dev: number; ino: number }, except: string): boolean` (a live chat host of another root on it); `private chatHostFor(root: string, binding: BoundFolder, before?: Promise<void>): Host` (the only place a chat's own host is made).

- [ ] **Step 1: Write the failing tests**, with `fakeSpawn(answering)` and `folders[ROOT_B] = folders[ROOT_A]`:
  - `it("says a folder is in use while a chat's host is on it, in its idle tail too, and not once it has gone")`: before any op `folderInUse(dev, ino)` false; after `run(resolve())` for ROOT_A, true; with `idleMs: 50`, after the host exits, false.
  - `it("knows a folder in use by its identity, whatever path names it")`: bind ROOT_B to another path string with ROOT_A's dev/ino; ROOT_A's host makes `folderInUse` true for that dev/ino.
  - `it("counts no thread's host on its copy as a chat in the folder")` in the "root bound to a thread's copy" block: a thread's host live on its copy leaves `folderInUse(folder's dev, ino)` false.
- [ ] **Step 2: Run** `npm test -- test/tool-hosts.test.ts -t "in use"`. Expected: FAIL (`folderInUse` is not a function).
- [ ] **Step 3: Implement.** `chatHostFor` calls `hostFor` and records the host in `chats`; `keep`'s exit handler drops it. `hostOf`'s chat branch calls `chatHostFor` where it called `hostFor`. Compare by `works.folder.dev` and `ino`, never by path (unlike `holderOf`, which stays as it is).
- [ ] **Step 4: Run** the file. Expected: PASS, nothing else changed.
- [ ] **Step 5: Commit** (`feat(desktop): know which chat's tool host is on a folder`), staging the five source files, the test and this plan.

### Task 3: The sheet warns about a folder in use, with New folder and Browse…

**Files:**
- Modify: `desktop/src/binding/binder.ts` (`FolderSheet` ~75, `FolderPrompts.confirmFolder` ~88, `BinderOptions`, `confirm` ~390-421, `prepareFolder`'s `finally`), `desktop/src/shell/prompt-content.ts:82-123` (`folderSheet`), `desktop/src/shell/prompts.ts:80-85`, `desktop/src/shell/device-stack.ts:191-206`
- Test: `desktop/test/prompt-content.test.ts`, `desktop/test/binder.test.ts` (its `User` fake's answer type gains `"new"`; the exact sheet objects there gain `busy: false`)

**Interfaces:**
- Consumes: `ToolLayer.folderInUse` (Task 2).
- Produces: `FolderSheet.busy: boolean`; `confirmFolder(...) : Promise<{ mode: Mode } | "change" | "new" | null>`; `BinderOptions.inUse?(dev: number, ino: number): boolean` (device-stack passes `(dev, ino) => tools.folderInUse?.(dev, ino) === true`).

- [ ] **Step 1: Write the failing tests.**
  - `prompt-content.test.ts`: `it("warns that another chat works in a busy folder, and offers New folder and Browse… beside Use this folder")`: `folderSheet({ ...SHEET, busy: true })` has the warning (exact copy) in `notes`, buttons `[cancel "Cancel", new "New folder", change "Browse…", accept "Use this folder" (allows)]`, focus `cancel`, and a greater height than `folderSheet(SHEET)`. And `it("is the sheet it always was for a folder not in use")`: `folderSheet({ ...SHEET, busy: false })` deep-equals today's content (buttons Cancel, Change…, Use this folder; no warning).
  - `binder.test.ts`: `it("says the folder is in use by another chat, for a new chat's sheet alone")`: `inUse: () => true`; `prepareFolder("last")` with notes bound gives `sheets[0].busy === true`; a project's sheet (`prepareProjectFolder`) and a thread label sheet (`prepareFolder("pick", ..., thread)`) give `busy: false`.
  - `it("offers a fresh folder at New folder, and removes every folder made and not taken")`: `inUse: () => true`, notes bound, `User([], ["new", "new", { mode: "free" }])`; the second sheet's folder is `.../YYYY-MM-DD`, the third `.../YYYY-MM-DD 2`, the result is the third; the second folder no longer exists, the third does. And with answers `["new", null]` no made folder is left.
  - `it("opens the folder dialog at New folder when no folder can be made")`: agent `".."` (no folder can be made, as the existing test uses), `User([notes], ["new", { mode: "free" }])`; one dialog opened; result `notes`.
- [ ] **Step 2: Run** `npm test -- test/prompt-content.test.ts test/binder.test.ts`. Expected: FAIL on the new cases.
- [ ] **Step 3: Implement.** `confirm` computes `busy` only for a chat's sheet (`thread === null && project === null`), from `checked.ok && this.options.inUse?.(checked.dev, checked.ino) === true`, once per sheet shown. Answer `"new"`: `newFolder(guards, agent)`; a folder made is added to a `made: string[]` that `prepareFolder` passes in, and offered next; none made opens the dialog as `"change"` does. `prepareFolder`'s `finally` marks the taken one `made` on its preparation and releases every other one made. `folderSheet` puts the warning in `notes` and adds 60 px per note as `forProject` does; the busy button set only when `sheet.busy`. `prompts.ts` maps button `new` to `"new"`.
- [ ] **Step 4: Run** the two files and `test/bridge.test.ts test/device-stack.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(desktop): warn on the folder sheet when another chat works in the folder`).

### Task 4: A chat's host lets its folder go at its turn's end

**Files:**
- Modify: `desktop/src/hosts/tool-hosts.ts` (`ToolHostsOptions` ~286, `chatHostFor`)
- Test: `desktop/test/tool-hosts.test.ts` (FakeHost block)

**Interfaces:**
- Consumes: `chatHostFor` (Task 2); the existing `Host.yieldFolder()` (~1177) and `Host.lives`.
- Produces: `ToolHostsOptions.following?(root: string, turnEnded: () => void): () => void`: asked once per chat host as it is made; the returned stop is called when that host exits. `turnEnded` makes that host (and no newer one of the root) yield its folder when no background process of the root lives; while one lives it keeps today's behaviour (idle time after they end).

- [ ] **Step 1: Write the failing tests** with a `following` stand-in that records each root's `turnEnded` and each stop:
  - `it("lets a chat's folder go as soon as its turn ends, not its idle time after")`: `idleMs: 60_000`; after `run(resolve())`, `ended[ROOT_A]()`; expect `sent(0, "stop") === 1` within 1 s.
  - `it("keeps the folder past a turn's end while a background process of the chat lives, then goes after the idle time")`: `idleMs: 200`; `processes(ROOT_A, { handles: [], live: 1 })`, `ended()`; after 400 ms no stop; `live: 0`; stop within 1 s.
  - `it("lets the folder go once an operation still running at the turn's end has been answered")`: a `readyOnly` host with one op pending; `ended()`; no stop; answer the op; stop follows.
  - `it("stops following a chat's turns once its host has gone, and a turn's end then touches no newer host")`: after the first host exits, its stop was called once; a new host for ROOT_A, then the first host's `turnEnded` is called again: the new host is not stopped.
  - `it("follows no thread's host on its copy")`: `following` is never asked for a thread root.
- [ ] **Step 2: Run** `npm test -- test/tool-hosts.test.ts -t "turn"`. Expected: FAIL.
- [ ] **Step 3: Implement** in `chatHostFor`: ask `following`, call `stop` on `host.exited`; the callback checks `this.hosts.get(root) === host && !host.lives` before `host.yieldFolder()`.
- [ ] **Step 4: Run** the whole file. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(desktop): let a chat's folder go at the end of its answer`).

### Task 5: The app follows each chat host's turns

**Files:**
- Modify: `desktop/src/shell/agent-events.ts:128-150` (`followChat`), `desktop/src/shell/main.ts:989-997` (the `appTools` options)
- Test: `desktop/test/agent-events.test.ts`

**Interfaces:**
- Produces: `export function followTurns(options: FollowOptions & { sessionId: string; onTurnEnd(): void }): { stop(): void; started(): boolean }`, the stream logic `followChat` has today (watch=1, `after`, stop on 404 and on `session.done` archived/gone). `followChat` becomes `followTurns` plus the title fetch, unchanged in behaviour.
- Consumes: `ToolHostsOptions.following` (Task 4).

- [ ] **Step 1: Write the failing test** `it("tells each turn's end of a chat at once, asking nothing more of the agent")` under a new `describe("a chat's turns, followed")`, with the file's fake api: one `session.complete` event calls `onTurnEnd` once and the api saw only the events path (no `/api/v1/sessions/<id>?agent_id=` read). Keep the existing followChat tests as they are.
- [ ] **Step 2: Run** `npm test -- test/agent-events.test.ts`. Expected: FAIL (`followTurns` not exported).
- [ ] **Step 3: Implement** `followTurns`, and in `main.ts` pass `following: (root, turnEnded) => ...` to `appTools`: with the app's sign-in (`signedIn`) at that moment, `followTurns({ api: session.api, agentId: agents.get()?.agentId ?? "", onError: report, sessionId: root, onTurnEnd: turnEnded }).stop`; with nobody signed in, a stop that does nothing (the idle time is then the fallback, as the spec says). Comment that a turn's end missed before the stream opened leaves the idle time to let the folder go.
- [ ] **Step 4: Run** the file and `npm run typecheck` if present, else `npm test -- test/device-stack.test.ts`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(desktop): follow the turns of each chat working in a folder`).

### Task 6: A chat waits for another chat's folder, up to 10 minutes

**Files:**
- Modify: `desktop/src/hosts/tool-hosts.ts` (constants near `LAND_WAIT_MS` ~61, `run`/`guarded` ~343-365 pass the operation id to `hostOf`, `hostOf`, `stopHosts` ~601, `keep`)
- Test: `desktop/test/tool-hosts.test.ts` (FakeHost block, and one `[slow]` test in the real-host block next to "lets one chat at a time work in a folder")

**Interfaces:**
- Consumes: `chats`, `chatOn`, `chatHostFor` (Task 2), `following` (Task 4), `once` and `Starting` (existing).
- Produces: `export const FOLDER_WAIT_MS = 10 * 60_000;` `export const WAITED_TOO_LONG: Outcome` (type `busy`, the exact limit message); `ToolHostsOptions.folderWaitMs?: number` (FOLDER_WAIT_MS unless a test says); private `queued = new Map<string, Starting>()` (a chat's wait, shared by every operation of the root that comes meanwhile, through `once`); private `queues = new Map<string, string[]>()` (roots waiting, by `${dev}-${ino}`, in order); private `nudge()` (wakes every wait to look again; called when a host exits, when a root leaves a queue, and in `stopHosts`); private `inTurn(root, binding, key, wanted): Promise<Host | Outcome>`.

Algorithm of the chat branch of `hostOf` (the part a signature does not fix):

```
key = `${binding.dev}-${binding.ino}`
wait when: queued.has(root) || chatOn(binding, root) || queues.get(key)?.length > 0
  -> once(queued, root, signal, (wanted) => inTurn(root, binding, key, wanted))
else -> chatHostFor(root, binding, before)

inTurn: push root on queues[key]; deadline = now + folderWaitMs
  loop:
    stopping          -> leave queue, nudge, QUITTING
    !wanted()         -> leave queue, nudge, CANCELLED
    head is root and !chatOn(binding, root)
                      -> shift, nudge, chatHostFor(root, binding)
    now >= deadline   -> leave queue, nudge, WAITED_TOO_LONG
    await nudge or deadline
```

Its host then starts with today's `LOCK_WAIT_MS`: the chat it waited for has exited, so the lock is free; a landing or a put-back that took it meanwhile is answered as today.

- [ ] **Step 1: Write the failing tests** (FakeHost, `folders[ROOT_B] = folders[ROOT_A]`, `following` stand-in as in Task 4):
  - `it("has a second chat's operation wait for the folder while the first chat's host is on it, and run once that one has gone")`: A runs; B's `resolve` stays pending for 300 ms and no second host was spawned; `ended[ROOT_A]()`; B's outcome is ok and its host is the second spawned.
  - `it("joins every operation of the waiting chat to its one wait")`: two B ops while waiting; after A goes, both ok, one host spawned for B.
  - `it("stops waiting after the folder wait's limit, saying why in words the agent can pass on")`: `folderWaitMs: 200`; B's outcome deep-equals `WAITED_TOO_LONG`, and the message is the exact copy; no host spawned for B.
  - `it("answers a waiting operation cancelled at once, and spawns nothing for it")`: abort B's signal; outcome `CANCELLED`; A goes; no second host.
  - `it("ends every wait at the quit, spawning nothing after")`: B waiting; `stop()`; B's outcome is the quitting answer (`unavailable`, message ending "the app is quitting"); spawned count unchanged.
  - `it("never has a chat wait on its own host, one still going included")`: with `idleMs: 60_000`, ROOT_A's second op while its first runs is answered by the same host (one spawned); then after `ended[ROOT_A]()` and before that host has exited (a FakeHost that exits 200 ms after `stop`), a new ROOT_A op spawns its new host at once, with no `waiting` told (Task 8 adds that assertion).
  - `it("keeps today's short wait behind a holder that is no chat of this app's")`: no chat host live, the FakeHost answers `start` with `failed { busy: true, message: <FolderBusy's message> }`: B is answered `unavailable` with that message at once, and `queues` stays empty (a second B op spawns a second host).
  - `it("puts no chat in the queue behind a thread's landing")`: with the existing landing helpers, a landing's host holds the folder; a chat's op spawns its host at once (it yields, as today), never waits in the queue.
  - `[slow]` `it("has a second chat wait for the folder a first chat's real host holds, past the lock's own wait, and go on once the first chat's turn has ended [slow]")`: real `forkHost`, `slowSearch` for A; B's `resolve` still pending after `LOCK_WAIT_MS + 2_000`; abort A's search, call A's `turnEnded`; B's outcome ok within 10 s. Timeout 60 s.
- [ ] **Step 2: Run** `npm test -- test/tool-hosts.test.ts -t "wait"` and `npm run test:slow -- test/tool-hosts.test.ts`. Expected: FAIL (B is answered at once by a second host).
- [ ] **Step 3: Implement** per the algorithm. `stopHosts` marks `queued` starts stopped and clears them like `starting`, then nudges. The existing "lets one chat at a time work in a folder" `[slow]` test changes meaning: B now waits; rewrite its expectation to B's operation being answered ok after A's host goes idle (`idleMs: 500`), and keep it.
- [ ] **Step 4: Run** the whole file, fast and slow, then `npm test` for the desktop. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(desktop): have a chat wait for the folder another chat works in`).

### Task 7: Several waiting chats get the folder in the order they asked

**Files:**
- Modify: `desktop/src/hosts/tool-hosts.ts` (only if a test below fails after Task 6)
- Test: `desktop/test/tool-hosts.test.ts`, with a third root `ROOT_C = "33333333-3333-4333-8333-333333333333"` on the same folder

**Interfaces:**
- Consumes: Task 6's queue.

- [ ] **Step 1: Write the tests:**
  - `it("gives the folder to waiting chats in the order they asked")`: A holds; B asks, then C; A's turn ends: B's host starts, C still waits; B's turn ends: C's starts.
  - `it("keeps a chat that asks again after its turn behind the chats already waiting")`: A holds, B waits; A's turn ends and, before B's host has started, A asks again: B is answered first, A waits for B's turn to end.
  - `it("moves the chats behind a waiter that was cancelled up")`: A holds, B waits, C waits; B cancelled; A's turn ends: C's host starts.
  - `it("lets a waiter past the limit go without holding up the others")`: per-chat limit; B's limit passes while C (asked later) still waits; C gets the folder when A goes.
- [ ] **Step 2: Run** `npm test -- test/tool-hosts.test.ts -t "order|behind|cancelled up|limit go"`. Expected: PASS if Task 6 followed the algorithm; any FAIL is a fairness bug to fix in `inTurn`/`hostOf` before going on (the newcomer rule `queues.get(key)?.length > 0` is the one that keeps A behind B).
- [ ] **Step 3: Fix** what failed, if anything.
- [ ] **Step 4: Run** the whole file. Expected: PASS.
- [ ] **Step 5: Commit** (`test(desktop): chats waiting for one folder get it in the order they asked`, or `fix(desktop): ...` if code changed).

### Task 8: The desktop tells the server an operation waits for the folder

**Files:**
- Modify: `desktop/src/link/protocol.ts` (`Welcome`, the welcome parse ~110-128, a new frame builder beside `opResult`), `desktop/src/hosts/tool-hosts.ts` (`ToolHostsOptions`, `hostOf`'s wait), `desktop/src/shell/device-stack.ts` (`DeviceStackOptions.tools` signature, `onWelcome`), `desktop/src/shell/main.ts:989` (pass `waiting` into `appTools`)
- Test: `desktop/test/client.test.ts` or the protocol's own test, `desktop/test/tool-hosts.test.ts`, `desktop/test/device-stack.test.ts`

**Interfaces:**
- Produces: `Welcome.waits: boolean` (`frame.waits === true`); `export function opWaiting(id: string, reason: "folder" | null): Record<string, unknown>` returning `{ type: "op_waiting", id, reason }`; `ToolHostsOptions.waiting?(operationId: string, reason: "folder" | null): void`, told `"folder"` when an operation enters a chat's wait and `null` when it leaves it, whatever its outcome; `DeviceStackOptions.tools(bindings, network, changed, waiting: (operationId: string, reason: "folder" | null) => void): ToolLayer`.
- Device stack: keeps the operations waiting now (id to reason); sends `opWaiting` on the link only while the last welcome said `waits`; sends every one still waiting again after each welcome that says `waits`.

- [ ] **Step 1: Write the failing tests:**
  - protocol: a welcome with `waits: true` parses `waits: true`; one without parses `false`; `opWaiting("x", "folder")` and `opWaiting("x", null)` give the frames above.
  - tool-hosts: `it("tells when each operation starts and stops waiting for the folder, and nothing for one that never waited")`: B's two ops while waiting give `[[idB1, "folder"], [idB2, "folder"]]`, then after A goes `[idB1, null], [idB2, null]`; A's own op told nothing. Also null after the limit and after a cancel.
  - device-stack (with its fake server): `it("tells a server that takes waits which operation waits for the folder, and again after it reconnects")` and `it("tells a server whose welcome says nothing of waits nothing")`.
- [ ] **Step 2: Run** those files. Expected: FAIL.
- [ ] **Step 3: Implement.** `hostOf` tells `waiting` around the `once` of the wait. main.ts forwards the stack's `waiting` into `appTools({ ..., waiting })`.
- [ ] **Step 4: Run** `npm test`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(desktop): tell the server while an operation waits for its folder`).

### Task 9: The server's link takes `op_waiting`

**Files:**
- Modify: `surogates/devices/link.py` (module docstring frame table 1-50, welcome ~636-645, `_heartbeats` ~785-799, a new `_Link.waiting`), `surogates/devices/operations.py` (a new `DeviceOperations.waiting`, a key helper beside `operation_channel` ~205)
- Test: `tests/test_device_link_delivery.py` (its `FakeOperations` gains `waiting`), `tests/integration/test_devices.py` (the Redis key)

**Interfaces:**
- Produces: welcome carries `"waits": True`; app frame `{"type": "op_waiting", "id", "reason": "folder" | None}`; `def waiting_key(operation_id: UUID) -> str` (`f"{operation_channel(operation_id)}:waiting"`); `async def DeviceOperations.waiting(self, operation_id: UUID, reason: str | None) -> None` (sets the key to the reason with an expiry of `FOLDER_WAIT_KEY_S = 15 * 60`, or deletes it); `async def _Link.waiting(self, frame: dict[str, Any]) -> None` (a malformed id or a reason other than `"folder"`/`None` closes with `CLOSE_PROTOCOL`; an id not in `self._delivered` is ignored; a Redis failure is logged and the link goes on).

- [ ] **Step 1: Write the failing tests:**
  - `test_an_operation_waiting_for_its_folder_is_recorded_while_it_waits_and_forgotten_after` (delivery file): a delivered op's `op_waiting` "folder" then None calls `FakeOperations.waiting` with `(id, "folder")` then `(id, None)`.
  - `test_a_wait_for_an_operation_not_delivered_on_this_link_changes_nothing`.
  - `test_a_malformed_wait_closes_the_link_as_a_protocol_error` (bad id; reason `"offline"`).
  - Integration: `test_the_folder_wait_key_holds_the_reason_until_it_is_cleared` against `redis_client`, with its TTL set.
  - The welcome test that asserts the welcome frame gains `"waits": True`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/test_device_link_delivery.py tests/integration/test_devices.py -k "wait or welcome"`. Expected: FAIL.
- [ ] **Step 3: Implement**, and document the frame in the docstring table: the app sends it only to a server whose welcome says `waits`, since any other unknown frame closes the link (4400).
- [ ] **Step 4: Run** the device link tests (`tests/test_device_link_*.py`). Expected: PASS.
- [ ] **Step 5: Commit** (`feat(devices): take an app's word that an operation waits for its folder`).

### Task 10: The worker says `device.waiting` with reason `folder`

**Files:**
- Modify: `surogates/devices/waits.py` (docstring, a `_folder: Counter[UUID]`, `folder_held`, `folder_free`, `back`), `surogates/devices/operations.py:668-688` (`_wait_watching`), `surogates/workstreams/derive.py:170-186` (`_working`)
- Test: `tests/integration/test_devices.py` (beside `test_a_session_waiting_for_an_absent_computer_says_so`, with `noticing_journal`), `tests/test_workstream_derive.py`

**Interfaces:**
- Consumes: `waiting_key` (Task 9).
- Produces: `async def DeviceWaitNotice.folder_held(self, request: OperationRequest) -> None` (first of the session's operations waiting: `device.waiting {device_id, device_name, reason: "folder"}`, unless its computer is away); `async def folder_free(self, request, *, announce: bool = True) -> None` (last one: `copying`'s notice again if a copy is being made, else `device.resumed`, unless away or not announced); `back()` says the folder wait again, before copying, when the computer returns while one still waits. `_wait_watching` reads the key each recheck (best effort, as `_online`), calls held/free on each change, and frees in its `finally` with `announce=not turn_detached()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_session_waiting_for_its_folder_says_so_and_says_it_goes_on_once_it_has_it`: the op's key set to "folder" while it waits gives `[("device.waiting", {..., "reason": "folder"}), ("device.resumed", {...})]` once answered.
  - `test_the_computer_away_while_an_operation_waits_for_its_folder_is_said_first_then_the_folder_wait_again`.
  - `test_two_operations_of_a_session_waiting_for_its_folder_say_so_once`.
  - derive: a thread whose latest wait is `reason="folder"` has status line `Waiting for another chat in the same folder` and is not in the group that waits on the user's computer (reason `None`, as `copying`).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/integration/test_devices.py tests/test_workstream_derive.py -k "folder"`. Expected: FAIL.
- [ ] **Step 3: Implement.** No server deadline is added or changed: an agent's operation already waits "however long that takes" (`DeviceOperations.run`), so a 10-minute folder wait is never cut off there; a file panel request keeps its own `READ_WITHIN_S` / `CHANGE_WITHIN_S` (`api/routes/workspace.py:123-124`), whose cancel ends the desktop's wait.
- [ ] **Step 4: Run** `tests/integration/test_devices.py tests/test_workstream_derive.py tests/integration/test_desktop_local_threads.py`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(devices): tell a chat's viewers it waits for another chat's folder`).

### Task 11: The chat SDK shows the folder wait (ui-kit repo)

**Files (in `/home/monica/invergent/ui-kit`):**
- Modify: `packages/agent-chat-react/src/types.ts:703-710` (the reason's comment), `packages/agent-chat-react/src/components/chat/chat-thread.tsx:2134-2136`
- Test: `packages/agent-chat-react/tests/agent-chat.test.tsx` (beside the `copying` case ~1883-1896)

- [ ] **Step 1: Branch.** ui-kit is on another branch with its own work: `cd /home/monica/invergent/ui-kit && git fetch origin && git worktree add ../ui-kit-folder-wait -b feat/device-wait-folder origin/main`; work in `../ui-kit-folder-wait`.
- [ ] **Step 2: Write the failing test** `it("says the chat waits for another chat working in the same folder")`: emit `device.waiting` with `reason: "folder"`; the `device-wait` region reads the decided notice copy; `device.resumed` empties it.
- [ ] **Step 3: Run** the package's `npm test -- tests/agent-chat.test.tsx`. Expected: FAIL (it reads the offline sentence).
- [ ] **Step 4: Implement** a third branch for `"folder"`. Run the test and the package's typecheck. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(agent-chat-react): show a chat waiting for another chat's folder`), push, open the PR. Task 14 waits for its release.

### Task 12: The page's folder choice, and older desktops

**Files:**
- Modify: `web/src/lib/local-chat.ts:59-91,127-167`, `web/src/lib/local-thread.ts` (type only), `web/src/features/chat/chat-page.tsx:147-196,294-299`
- Test: `scripts/web-local-chat.test.mjs`, `scripts/web-chat-page.test.mjs` (its `./new-chat-place` stub records its props on `globalThis.chatPage.place`)

**Interfaces:**
- Consumes: `DesktopFolderChoice`, `offersNewFolder` (Task 1).
- Produces in `local-chat.ts`: `export const FOLDER_NOTE = "You confirm the folder after your first message.";` `export const FOLDER_CHOICES: ReadonlyArray<{ value: DesktopFolderChoice; label: string; hint: string; shown: string }>` (the three rows of copy, in menu order new, last, pick); `newChatPlace(device, agent, choice: DesktopFolderChoice, menu: boolean)`: local text is `"Works in"` when `menu`, today's sentences otherwise; `createChat(desktop, agent, choice: DesktopFolderChoice, shown, api)` passes the choice through. The page's `menu` is `getDesktop()?.offersNewFolder === true`; its choice starts at `"new"` with the menu and `"last"` without, and goes back to that start for each new chat (the `[sessionId]` effect).

- [ ] **Step 1: Write the failing tests:**
  - local-chat: `test("offers a new folder, the last folder or one chosen when sent, in that order, with their words")` (deep-equals `FOLDER_CHOICES` against the copy table); `test("says Works in before the menu, and today's sentences to a desktop without one")`; `test("asks the desktop for the folder kind chosen, a new one too")` (`createChat` with `"new"` calls `prepareFolder("new")`).
  - chat-page: `test("starts each new chat on a new folder of its own where the desktop offers one, and on the last folder where it does not")`: with `offersNewFolder: true` the stub sees `choice: "new"`, `menu: true`, and sending calls `prepareFolder("new")`; without it, `choice: "last"`, `menu: false`; after `onChoice("pick")` and a chat opened and left, the choice is `"new"` again.
- [ ] **Step 2: Run** `cd web && npm run test:local-chat && npm run test:chat-page`. Expected: FAIL.
- [ ] **Step 3: Implement.** `NewChatPlace` gets the new props in Task 13; here pass `menu` and the wider choice through, keeping today's link rendering for `menu === false`.
- [ ] **Step 4: Run** both scripts, `npm run test:local-thread`, `npm run typecheck`, `npm run test:copy`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(web): start a new chat on a new folder where the desktop offers one`).

### Task 13: The folder menu under the composer

**Files:**
- Modify: `web/src/features/chat/new-chat-place.tsx`, `web/package.json` (script `test:new-chat-place`)
- Create: `scripts/web-new-chat-place.test.mjs` (the Vite + happy-dom harness of `scripts/web-chat-page.test.mjs`, loading the real component)

**Interfaces:**
- Consumes: `FOLDER_CHOICES`, `FOLDER_NOTE` (Task 12).
- Produces: `NewChatPlace({ place, choice, onChoice, menu }: { place: { local: boolean; text: string }; choice: DesktopFolderChoice; onChoice(choice: DesktopFolderChoice): void; menu: boolean })`. With `menu`: laptop icon, `Works in`, a button (selected row's icon, its `shown` label, `ChevronDownIcon`) that opens `DropdownMenu` from `@invergent/ui-sdk/dropdown-menu` upward (`side="top"`), a `DropdownMenuRadioGroup` of three `DropdownMenuRadioItem`s (icon, label, hint on its own line; the radio item's own check marks the selected one), then `·` and `FOLDER_NOTE`. Without `menu`: exactly today's markup. Icons by value in the component: `new` FolderPlusIcon, `last` HistoryIcon, `pick` FolderOpenIcon.

- [ ] **Step 1: Write the failing tests:** `test("shows a new folder as chosen, with its icon")` (button text `a new folder`, an svg with class `lucide-folder-plus`, the note); `test("lists the three kinds of folder with their hints, the chosen one checked, and tells the page which was chosen")` (open the menu, three items in order with labels and hints, `aria-checked="true"` on the chosen one; choosing `The last folder` calls `onChoice("last")`; rendered again with `"last"` the button reads `the last folder` with `lucide-history`); `test("shows today's line and link to a desktop that offers no new folder")`.
- [ ] **Step 2: Run** `cd web && npm run test:new-chat-place`. Expected: FAIL.
- [ ] **Step 3: Implement**, using the page's existing text sizes (`text-xs text-muted-foreground`) as the current line does.
- [ ] **Step 4: Run** it, `npm run test:chat-page`, `npm run typecheck`, `npm run test:copy`, `npm run biome:check`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat(web): choose a new, the last or any folder under a new chat's composer`).

### Task 14: The web takes the SDK that shows the folder wait

**Files:**
- Modify: `web/package.json` (`@invergent/agent-chat-react`), `web/package-lock.json`

- [ ] **Step 1:** Once Task 11's PR is merged and released, `cd web && npm install @invergent/agent-chat-react@<released version>`; check `node_modules/@invergent/agent-chat-react/dist/index.js` holds the folder notice copy.
- [ ] **Step 2: Run** `npm run typecheck`, `npm run test:chat-page`, `npm run build`. Expected: PASS.
- [ ] **Step 3: Commit** (`build(web): take the agent chat that shows a chat waiting for its folder`). This branch does not merge before this commit: without it the page would say a folder wait as the computer being offline.

---

## Live verification checklist (Ubuntu laptop and Windows 11 VM)

Not a code task. Run each on both, with a desktop built from this branch against a local server from this branch (`start-local.sh`, office VPN), and note the result per platform.

- [ ] A new chat defaults to "a new folder"; the sheet offers `~/Surogate/<agent>/YYYY-MM-DD` (then ` 2`, ` 3` the same day).
- [ ] "The last folder" offers the last bound folder; with it deleted, a new folder. "Choose a folder…" opens the system dialog after sending.
- [ ] A new folder cancelled on the sheet is removed; one with a file put in it meanwhile stays. "New folder" pressed twice on a busy sheet leaves only the taken folder.
- [ ] A reopened old chat keeps its folder, across an app restart.
- [ ] The choice resets to "a new folder" for the next new chat.
- [ ] Busy sheet: while chat A works in a folder (or within its idle tail), "the last folder" for B shows the warning and Cancel · New folder · Browse… · Use this folder; New folder offers a fresh one; Browse… opens the dialog; a folder not in use shows today's sheet.
- [ ] Two chats in one folder: A runs a long command; B asks for a file list, shows the folder-wait notice in the chat and in the sidebar's status, and continues by itself when A's answer ends, the notice gone. Then A asks while B works and waits in turn.
- [ ] A starts a background command (`start`) and ends its answer: B keeps waiting until the command ends, then the idle time.
- [ ] A's answer runs past 10 minutes: B stops with the exact limit message, and the agent passes it on.
- [ ] Three chats in one folder take turns in the order they asked.
- [ ] B's file panel opened while B waits: it answers once B has the folder, or says the computer did not answer in time after 60 s.
- [ ] Closing A's window while B waits: B goes on once A's answer ends. Quitting the app while B waits: B's operation ends; after restart, B's next message works.
- [ ] Not signed in to the app (device linked): turn taking still happens, by the idle time.
- [ ] An older installed desktop (the colleagues' release) with this page shows today's line and link, and new chats work as before.
- [ ] This desktop against a server without this branch: no `op_waiting` is sent (no 4400 reconnect loop in the link log), and waiting still works without the notice.
- [ ] A project's threads (copies) and their landings work as before, and a chat behind a landing gets today's message.
