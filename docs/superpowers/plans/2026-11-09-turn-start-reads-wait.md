# Turn-start reads wait for a folder another chat holds: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When another chat holds a local folder, the harness's own reads of that folder outside a tool call (the project context and the turn mark at a turn's start, the turn's file walk and a promoted fence at its end) wait their turn, up to the 10-minute folder limit, instead of giving up after 10 s. When no other chat holds the folder, today's 10 s deadline is unchanged.

**Architecture:** One helper on the server. `session_files` gains `within: float | None`: it runs the whole request under `asyncio.timeout(within)` and, when `within` is given, makes its `DeviceOperations` with a `DeviceWaitNotice` and a new `FolderPatience` over that timeout. `DeviceOperations._wait_watching` already polls `waiting_key(operation_id)` every recheck interval; while the key says `"folder"` (and the computer is online) it pushes the deadline two intervals ahead, and once the wait ends it pushes it to `within` from then. `FolderPatience` never pushes past `within + FOLDER_WAIT_S` from its start. The four harness callers drop their own `asyncio.timeout(HARNESS_WITHIN_S)` and pass `within=HARNESS_WITHIN_S`. No desktop change: the desktop already sends `op_waiting` for every operation it queues for a folder, these reads included.

**Tech Stack:** Python 3.12 (asyncio `Timeout.reschedule`), Redis, pytest with testcontainers; the desktop's vitest suite as a guard only.

**Spec:** `docs/superpowers/specs/2026-11-06-new-chat-own-folder-design.md`, section 6 of its Addendum (read it whole, with section 4).

## Todo

- [x] Task 1: A deadline that a folder wait holds off (`FolderPatience`, `DeviceOperations(patience=)`)
- [x] Task 2: `session_files(within=)` waits a folder's turn and says so
- [ ] Task 3: The harness's folder requests ask with `within`

## Global Constraints

- Repo `/home/monica/invergent/surogates`, branch `fix/desktop-new-chat-folder` (already checked out; stay on it).
- Update the Todo checkboxes above in the same commit as each task.
- Commit titles: Conventional Commits `type(scope): subject` (scopes here: `devices`, `harness`). Never mention Claude or AI; no `Co-Authored-By` or any trailer. Check `git log -1 --format=%B` after each commit.
- Never reference plan, task or step numbers (or private bug ids) in code comments, commit messages, test names or any committed artifact.
- Production code only: no TODOs, stubs or placeholders. TDD: the failing test first, run and seen failing for the right reason, then the code.
- Stage files explicitly by path. Never `git add -A` / `git add .`. Never `git stash`, never `git checkout` over uncommitted work.
- No em or en dashes anywhere (comments, docstrings, log text).
- Comments and docstrings: the codebase's own style, plain terse sentences saying what a thing is or why; no banners, no "NOTE:".
- Python tests: `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" <paths>` from the repo root. `tests/integration/` uses Docker test containers: if Docker is refused, prefix with `sg docker -c '...'`.
- Desktop guard (no desktop code changes): `cd desktop && PATH="$HOME/.local/node/bin:$PATH" npm run build && npx vitest run test/tool-hosts.test.ts test/device-stack.test.ts`.
- Values: `HARNESS_WITHIN_S = 10.0` (unchanged), new `FOLDER_WAIT_S = 10 * 60` in `surogates/devices/operations.py` (the desktop's `FOLDER_WAIT_MS`, `desktop/src/hosts/tool-hosts.ts:64`), `RECHECK_INTERVAL_S = 5.0` and `WAIT_GRACE_S = 2.0` unchanged. The hold-off lead is two recheck intervals (10 s), so a poll missed or a link that drops and comes back within a few seconds does not end the wait.
- Out of scope, unchanged: the file panel (`surogates/api/routes/workspace.py:123-124,362-420`, `READ_WITHIN_S` / `CHANGE_WITHIN_S`; it never passes `within`, so it gets no notice and no hold-off), tool calls (`surogates/devices/sandbox.py`), the office and artifact routes that call `session_files` without a deadline.

## Review Focus

- The deadline is only ever pushed later, never earlier, and never past `within + FOLDER_WAIT_S` from the start; a `Timeout` that already expired is never rescheduled (it would raise `RuntimeError`). (Task 1 tests.)
- An operation answered while the server still believed it waited leaves the next operations of the same request a full `within`: the `finally` of `_wait_watching` frees the patience as well as the notice. (Task 1 test `..._leaves_the_next_the_whole_deadline`.)
- The computer offline stops the hold-off even if the key lingers, so an away computer cannot stall a turn start past about `within` plus one lead. (Task 1 test.)
- `session_files` without `within` builds `DeviceOperations(session_factory, redis)` exactly as before: no notice from the API process, whose counts in `surogates/devices/waits.py` are per process and would fight the worker's. (Task 2 test.)
- Import cycle: `surogates.session.files` is imported by the harness; `DeviceWaitNotice` and `SessionStore` are imported inside the function, as `surogates/devices/sandbox.py:220-223` does.
- A timeout still cancels the operation (`DeviceOperations.run`'s `except asyncio.CancelledError` path, `_cancel_own`), which takes it out of the desktop's folder queue.

---

### Task 1: A deadline that a folder wait holds off

**Files:**
- Modify: `surogates/devices/operations.py` (a `FOLDER_WAIT_S` constant beside `FOLDER_WAIT_KEY_S` ~210; a `FolderPatience` class after `waiting_key` ~215; `DeviceOperations.__init__` ~611-622 gains `patience`; `_wait_watching` ~706-737)
- Test: `tests/integration/test_devices.py` (beside the folder-wait tests ~3028-3125)

**Interfaces:**
- Produces:
  - `FOLDER_WAIT_S = 10 * 60`
  - `class FolderPatience:` `__init__(self, deadline: asyncio.Timeout, within_s: float) -> None` (records `latest = loop.time() + within_s + FOLDER_WAIT_S`, reading the module constant at call time so a test can patch it); `held(self, ahead_s: float) -> None`; `freed(self) -> None` (same as `held(within_s)`). Both push the deadline to `min(latest, now + ahead_s)` but never earlier than its current `when()`, and do nothing once `deadline.expired()`.
  - `DeviceOperations.__init__(..., notice: DeviceWaitNotice | None = None, patience: FolderPatience | None = None)`. The patience is heeded only where the notice is given (`_wait_watching` watches only then); document it so.
  - `_wait_watching`: on each poll, `if held is True and online is not False and self._patience is not None: self._patience.held(2 * self._recheck_interval_s)`; where `folder` turns False (in the loop and in the `finally`), `self._patience.freed()` beside `folder_free`.

Sketch (fit it to the file's style):

```python
FOLDER_WAIT_S = 10 * 60


class FolderPatience:
    """A request's deadline, held off while the app says one of its operations waits its turn for a folder.

    Never past *within_s* plus FOLDER_WAIT_S from its start: the app gives up at that
    limit itself, and a wait it never took back must not hold a turn for ever.
    """

    def __init__(self, deadline: asyncio.Timeout, within_s: float) -> None:
        self._deadline = deadline
        self._within_s = within_s
        self._latest = asyncio.get_running_loop().time() + within_s + FOLDER_WAIT_S

    def held(self, ahead_s: float) -> None:
        """An operation still waits for its folder: the deadline is at least *ahead_s* away."""
        if self._deadline.expired():
            return
        when = min(self._latest, asyncio.get_running_loop().time() + ahead_s)
        current = self._deadline.when()
        self._deadline.reschedule(when if current is None else max(current, when))

    def freed(self) -> None:
        """Its wait ended: what is left of the request has *within_s* from now."""
        self.held(self._within_s)
```

Test helper, in the test file:

```python
def patient_journal(session_factory, redis_client, deadline, within) -> DeviceOperations:
    return DeviceOperations(
        session_factory, redis_client, recheck_interval_s=0.1,
        notice=DeviceWaitNotice(SessionStore(session_factory, redis_client), session_factory),
        patience=FolderPatience(deadline, within),
    )
```

Each test: `monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)`, `await DevicePresence(redis_client).claim(rig.device_id, "holder")` (online, the fake laptop not connected, so nothing answers), the read run as a task `async with asyncio.timeout(WITHIN) as deadline: return await patient_journal(..., deadline, WITHIN).run(request)` with `WITHIN = 1.0`, the op found with `operation_of(rig.ops, rig, request)`, and the wait said with `await rig.ops.waiting(op.id, "folder")`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_deadline_stands_still_while_its_operation_waits_for_its_folder`: key set; `await asyncio.sleep(2.5)`; `assert not reading.done()`; then `rig.ops.complete(rig.device_id, 1, op.id, op.digest, {"ok": True}) == "completed"` and `await asyncio.wait_for(reading, 5.0) == {"ok": True}`; events `== [folder_waiting_for(rig), resumed_from(rig)]`.
  - `test_a_deadline_runs_again_once_the_folder_is_free`: key set; sleep 2.0; `freed = loop.time()`; `await rig.ops.waiting(op.id, None)`; `with pytest.raises(TimeoutError): await asyncio.wait_for(reading, 5.0)`; `assert WITHIN - 0.2 <= loop.time() - freed <= WITHIN + 1.0`.
  - `test_a_deadline_with_no_folder_wait_runs_out_as_before`: no key; `TimeoutError`, and the whole run took `< WITHIN + 0.5`; no device events.
  - `test_a_folder_wait_holds_a_deadline_off_no_longer_than_the_folder_limit`: `monkeypatch.setattr(operations_module, "FOLDER_WAIT_S", 1.0)`; key set and never cleared; `TimeoutError` with elapsed between `WITHIN + 1.0 - 0.2` and `WITHIN + 1.0 + 0.5`.
  - `test_a_computer_gone_away_ends_its_folder_wait_patience`: key set; sleep 1.5; `await DevicePresence(redis_client).release(rig.device_id, "holder")` with the key left in place; `TimeoutError` within `2 * 0.1 + WITHIN + 0.5` of the release.
  - `test_an_operation_answered_after_its_folder_wait_leaves_the_next_the_whole_deadline`: one task runs two requests in turn under the same deadline and journal; the first is held (key set, sleep 2.0) then completed; the second is never answered; `TimeoutError`, raised at least `WITHIN - 0.2` after the first was completed.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/integration/test_devices.py -k "deadline or patience"`. Expected: FAIL on `ImportError: cannot import name 'FolderPatience'` (then, once the class exists with no wiring, the stand-still and hold-off tests fail on `TimeoutError`).
- [ ] **Step 3: Implement** as above. Keep `_wait_watching`'s early `return await waiter` when `self._notice is None`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/integration/test_devices.py -k "wait or folder or deadline or patience"`. Expected: PASS, the existing folder-wait and away tests included.
- [ ] **Step 5: Commit** (`feat(devices): hold a request's deadline off while its operation waits for its folder`).

### Task 2: `session_files(within=)` waits a folder's turn and says so

**Files:**
- Modify: `surogates/session/files.py` (`session_files` 80-161: the `within` keyword, its docstring; the `HARNESS_WITHIN_S` comment 37-42 says it is held off while another chat holds the folder)
- Test: `tests/integration/test_devices.py` (beside `test_a_prompt_waits_no_longer_than_its_bound_on_a_computer_that_does_not_answer` ~3934)

**Interfaces:**
- Consumes: `FolderPatience`, `DeviceOperations(patience=)` (Task 1).
- Produces: `session_files(session, *, storage, session_factory, redis, request_id=None, change=None, access=None, within: float | None = None)`. The whole body, cloud and device, runs under `async with asyncio.timeout(within) as deadline:` (`asyncio.timeout(None)` is no deadline, so the no-`within` path keeps its behaviour). On a device with `within` given:

```python
    if within is None:
        operations = DeviceOperations(session_factory, redis)
    else:
        # Imported here: both reach surogates.session, whose store imports the harness, which imports this module.
        from surogates.devices.waits import DeviceWaitNotice
        from surogates.session.store import SessionStore

        operations = DeviceOperations(
            session_factory, redis,
            notice=DeviceWaitNotice(SessionStore(session_factory, redis), session_factory),
            patience=FolderPatience(deadline, within),
        )
```

  Docstring addition: "*within*, the harness's deadline for a request outside a tool call: past it the request raises TimeoutError, but not while the computer says one of its operations waits its turn for a folder another chat holds, up to FOLDER_WAIT_S more; such a wait is told to the session's viewers as a tool call's is."

Tests use a laptop that is online but answers nothing: `await rig.laptop.connect(); rig.laptop.hold = True`, `monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)`, `monkeypatch.setattr(files_module, "DeviceOperations", functools.partial(DeviceOperations, recheck_interval_s=0.1))` (`from surogates.session import files as files_module`), and the read as a task:

```python
async def read(within):
    async with session_files(session, storage=None, session_factory=session_factory, redis=redis_client, within=within) as files:
        return await files.stat(await files.resolve("AGENTS.md"))
```

The op is the pending one whose `invocation_id.startswith(REQUEST_PREFIX)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_harness_request_waits_for_a_folder_another_chat_holds_and_says_so`: `within=1.0`; key set on the op; sleep 2.5; `assert not reading.done()`; events `== [folder_waiting_for(rig)]`; `await rig.ops.waiting(op.id, None)`; `TimeoutError` (the laptop still answers nothing); the last event `== resumed_from(rig)`.
  - `test_a_request_without_the_harness_deadline_says_nothing_of_a_folder_wait`: `async with asyncio.timeout(1.0)` around `read(None)`; key set; `TimeoutError` within 1.5 s; no device events (guards the file panel path).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/integration/test_devices.py -k "harness_request or harness_deadline"`. Expected: FAIL on `TypeError: session_files() got an unexpected keyword argument 'within'`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/integration/test_devices.py tests/test_harness_folder_git.py tests/api` (the file panel's tests in `tests/api` and the integration file's `workspace` tests still pass). Expected: PASS.
- [ ] **Step 5: Commit** (`feat(devices): let a harness request on a folder wait its turn behind another chat`).

### Task 3: The harness's folder requests ask with `within`

**Files:**
- Modify: `surogates/harness/loop_context_replay.py:1024-1036` (`_read_folder_context`), `surogates/harness/loop_artifact_completion.py:324-330` (`_promote_fenced_artifacts`), `:876-893` (`_walk_folder`), `:895-916` (`_folder_cursor`). Each `async with asyncio.timeout(HARNESS_WITHIN_S), session_files(...)` becomes `async with session_files(..., within=HARNESS_WITHIN_S)`. `HARNESS_WITHIN_S` stays imported in each module (read at call time, so the existing tests that patch it per module keep working). Docstrings that say "within ``HARNESS_WITHIN_S``" add "or, while another chat holds the folder, once it has had its turn".
- Test: `tests/test_loop_turn_id.py` (beside `test_a_folders_harness_request_that_gives_up_logs_by_why` ~451)

**Interfaces:**
- Consumes: `session_files(within=)` (Task 2).

- [ ] **Step 1: Write the failing test** `test_every_harness_request_on_a_folder_asks_with_the_harness_deadline`: a fake `session_files` that records `kwargs.get("within")` then raises `ComputerAway("the laptop", revoked=False)`, patched into `loop_context_replay` and `loop_artifact_completion`; call `ContextReplayMixin._read_folder_context`, `ArtifactCompletionMixin._folder_cursor`, `ArtifactCompletionMixin._walk_folder(..., since="1")` and `ArtifactCompletionMixin._promote_fenced_artifacts(harness, session, "```svg\n<svg></svg>\n```", [])` with `harness = SimpleNamespace(_api_client=None, _storage=None, _session_factory=None, _redis=None, _store=None)` and `session = SimpleNamespace(id=uuid4(), config={"execution": {"kind": "device", "device_id": str(uuid4())}})`; `assert asked == [HARNESS_WITHIN_S] * 4`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/test_loop_turn_id.py -k "harness_request or harness_deadline"`. Expected: FAIL, `asked == [None, None, None, None]`.
- [ ] **Step 3: Implement.** Drop the `asyncio` import from a module only if nothing else there uses it.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts="" tests/test_loop_turn_id.py tests/test_harness_folder_git.py tests/integration/test_devices.py`. Expected: PASS, `test_a_prompt_waits_no_longer_than_its_bound_on_a_computer_that_does_not_answer` and `test_a_fenced_svg_waits_no_longer_than_its_bound_on_a_computer_that_does_not_answer` included (no folder wait there, so the bound holds as before). Then the desktop guard from Global Constraints. Expected: PASS.
- [ ] **Step 5: Commit** (`fix(harness): wait a folder's turn for its project context and turn mark`).

## Live verification (Ubuntu laptop, desktop run from source)

Not a code task. Local server from this branch (`start-local.sh`, office VPN), desktop from source: `cd desktop && PATH="$HOME/.local/node/bin:$PATH" npm start`. Put an `AGENTS.md` in the shared folder with one line the agent will obey visibly (say, "End every answer with the word PINEAPPLE."). Watch the worker log for `could not be read` and `did not say where this turn began`.

- [ ] Two chats, A and B, in one folder. A runs a long command (`sleep 90` and then lists the folder). While A works, send B a message that makes it change a file. B shows the folder-wait notice in the chat and the sidebar from its turn's start, not only from its first tool call; once A's answer ends B goes on by itself, its answer ends with PINEAPPLE, and its turn summary lists the file it changed. The worker log has neither line above.
- [ ] The same with no other chat on the folder: B's turn starts at once, as before (no notice, no delay).
- [ ] A's answer runs past 10 minutes (a long `sleep` loop): B's turn start gives up with the desktop's limit, the worker logs the read's failure, and B's turn still runs, without the project context; its first tool call then waits for the folder again, and gives the limit message if A is still not done.
- [ ] B waiting at its turn start, quit the desktop app: within about 10 s B's turn goes on without the project context and lists no files, as for an away computer today. Start the app again: B's next message works.
- [ ] B waiting at its turn start, press Stop in B: the turn stops, and A's next turn is not delayed by a read of B's left queued (the desktop drops it).
- [ ] Turn off Wi-Fi for 3 s while B waits: B keeps waiting and goes on once A is done.
- [ ] An older installed desktop (the colleagues' release) against this server: with two chats on one folder, B's turn start gives up after 10 s exactly as before, and no error appears in the link log.
- [ ] B's file panel opened while B waits: it behaves as today (answers once B has the folder, or says the computer did not answer in time after 60 s).
