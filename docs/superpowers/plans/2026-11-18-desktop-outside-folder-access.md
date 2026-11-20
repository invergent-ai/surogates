# Desktop: working with files outside the chat's folder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** stop desktop agents presenting their private home as the user's profile (Part 0), and let them read, copy, move (via the Recycle Bin) and put back files outside the chat's folder behind the app's own approval windows (Part A).

**Architecture:**
- **Part 0** changes the device-chat instructions (server) and the guest HOME mount point (desktop).
- **Part A** adds a main-process `ToolLayer` in the desktop app that owns every operation on paths outside the chat's folder (`outside.*` kinds), with its own path checks, per-chat read grants in the journal, and new approval windows (batch checkboxes, a distinct Recycle Bin window) admitted before free mode. On the server, `DeviceWorkspaceIO` routes absolute paths outside the folder to `outside.*` reads, so the existing read tools work there, and four new device-only tools cover grant, copy, trash and put-back.

**Tech Stack:** TypeScript + Electron + vitest (`desktop/`); Python 3 + pytest (`surogates/`).

**Spec:** `docs/superpowers/specs/2026-11-18-desktop-outside-folder-access-design.md`

## Global Constraints

- **Base:** branch `feat/desktop-outside-folder` (master `c195f8cc4` + the spec commit), worktree `/home/monica/invergent/.worktrees/desktop-outside-folder`.
- **No database migrations:** grants and copy records live in the desktop journal (SQLite, `CREATE TABLE IF NOT EXISTS`); the server stores nothing new.
- **Free mode never covers `outside.*` kinds:** they are admitted before `asking()`'s free-mode return (`desktop/src/binding/approvals.ts:645`).
- **Every operation outside the chat's folder runs in the Electron main process** with its own checks, never by widening the file helper (Windows has no OS sandbox: `desktop/src/hosts/srt-win32.ts`).
- **Paths outside the folder:**
  - links, junctions and reparse points are never followed out of a granted folder: open through `fdPaths` (`desktop/src/files/fd-paths.ts:54`), the same walk the helper uses;
  - grants are refused exactly where `checkFolder` refuses a chat's folder (`desktop/src/binding/folder.ts:130`).
- **Deleting a user's original:** only via `shell.trashItem`, never `unlink`, and only inside `outside.trash` or `outside.put_back`.
- **A copy never overwrites:** a taken name gets " (1)", " (2)", … before the extension (the spec says " (1)", unlike downloads' " (2)").
- **Project threads are refused:** the predicate is `bindings.get(op.sessionId)?.history !== undefined` on the desktop and `copy_of(config) is not None` on the server, with the message `"Files outside the chat's folder can be used from the main chat, not from a project's thread."`.
- **Window text is English,** as every existing window: the app has no translations (`desktop/src/shell/prompt-content.ts`). No em dashes in any user-facing text.
- **Not in this plan:**
  - content search (ripgrep) outside the folder: `search_files` outside answers "Search outside the chat's folder is by name: use list_files with a pattern.";
  - office tools on outside files (the agent copies the file in first);
  - the terminal outside the folder.
- **Commits:** conventional titles `feat(desktop): …` / `feat(devices): …`, no AI trailers, stage files by name (never `git add -A`), never `git stash`.
- **Tests:**
  - desktop: `cd desktop && npm run build && npx vitest run test/<file>.test.ts`;
  - Python: one file at a time, `just test tests/<file>.py -n0` (the laptop can't take the full suite);
  - VM tests (`desktop/test/vm/`) need KVM; run them only in Task 2 and Task 13.

## Review Focus

1. **A link or junction inside a granted folder pointing outside it** (or to a credential folder) must be refused for read, copy and trash, not followed. Test in Task 4: `refuses a path that leads out of the grant through a link`.
2. **The original changed between copy-in and put-back** (the user edited it in Excel): put-back is refused and the working copy is kept. Test in Task 10: `refuses to put back when the original changed since it was copied`.
3. **The original is locked by another program during put-back** (Windows `EBUSY`/`EPERM`): nothing is trashed or replaced, and the working copy is kept. Test in Task 10: `leaves everything as it was when the original cannot be replaced`.
4. **A destination name already taken:** the copy is saved as "name (1).ext" and the outcome names the file actually written. Test in Task 9: `numbers a copy whose name is taken instead of overwriting`.
5. **A chat in free mode:** every `outside.*` window still shows. Test in Task 7: `asks for outside kinds even when the chat works freely`.

---

## Part 0

### Task 1: Device-chat workspace instructions (server)

**Files:**
- Create: `surogates/harness/prompts/identity/workspace_rules_device.md`
- Modify: `surogates/harness/prompt.py:956-959`, `surogates/devices/binding.py` (add `shown_folder`)
- Test: `tests/test_prompt_workspace_block.py`

**Interfaces:**
- Produces: `surogates.devices.binding.shown_folder(path: str) -> str`. It maps `/mnt/<letter>/a/b` to `<LETTER>:\a\b`, mirroring `windowsHostPath` in `desktop/src/session-path.ts`, and leaves every other path as it is.
- Produces: the prompt key `identity/workspace_rules_device`.

- [ ] **Step 1: Write the failing tests** in `tests/test_prompt_workspace_block.py`. Reuse `_context`, adding `config` overrides through a new keyword `execution=None` that sets `config["execution"]`.

```python
DEVICE = {"kind": "device", "device_id": str(uuid4()), "device_name": "LAPTOP"}

def test_a_device_chat_names_its_folder_not_home():
    section = _context({"read_file"}, workspace_path="/mnt/c/Users/Ana/Scans", execution=DEVICE)
    assert "- **Workspace**: the chat's folder on the user's computer, `C:\\Users\\Ana\\Scans` (your working directory)" in section
    assert "`$HOME` (your working directory)" not in section
    assert "private scratch area" in section
    assert "not the user's Desktop" in section

def test_a_linux_device_chat_shows_its_path_as_it_is():
    section = _context({"read_file"}, workspace_path="/home/ana/proj", execution=DEVICE)
    assert "`/home/ana/proj` (your working directory)" in section

def test_shown_folder():
    from surogates.devices.binding import shown_folder
    assert shown_folder("/mnt/d/Profiles/x y/z") == "D:\\Profiles\\x y\\z"
    assert shown_folder("/mnt/c") == "C:\\"
    assert shown_folder("/home/ana") == "/home/ana"
```

- [ ] **Step 2: Run** `just test tests/test_prompt_workspace_block.py -n0`. Expected: the 3 new tests FAIL; the 4 old ones pass.
- [ ] **Step 3: Implement.**
  - In `prompt.py`, when `device_of(self._session.config) is not None`, append the workspace line with `shown_folder(workspace)` and `self._prompts.get("identity/workspace_rules_device")`; otherwise keep today's two lines.
  - The new `.md` copies `workspace_rules.md`'s frontmatter shape, with `applies_when: a device session` and this body:

```
## Workspace
Your working folder is the chat's folder on the user's computer, named above. Give the file tools paths relative to it. `~` and `$HOME` are a private scratch area of this chat that the user cannot see: they are not the user's Desktop, Downloads or profile, so never tell the user a file is there. Tell the user a file is somewhere only when it is in the chat's folder, in a folder outside it reached with the outside-folder tools, in OneDrive/SharePoint, or in Outlook. Never read credential files (`~/.ssh`, `~/.aws`, `~/.kube`).
```

  The outside-folder sentence is extended in Task 12.
- [ ] **Step 4: Run** the same command. Expected: 7 passed.
- [ ] **Step 5: Commit** `feat(prompt): name a desktop chat's folder and say its home is private`.

### Task 2: The private home at a neutral path (desktop)

**Files:**
- Modify: `desktop/src/host-user.ts`
- Test: `desktop/test/host-user.test.ts`, `desktop/test/vm/guest.test.ts`, `desktop/test/vm/guest-support.ts:156`

**Interfaces:**
- Produces: `export const GUEST_HOME = "/surogate/home"` in `desktop/src/host-user.ts`. `hostUser().home === GUEST_HOME` on every OS; uid, gid and name are unchanged.

- [ ] **Step 1: Update the tests.**
  - In `host-user.test.ts`, expect `home: GUEST_HOME` on Linux, macOS and Windows (both Windows cases).
  - In `guest-support.ts`, set `USER.home` to `GUEST_HOME`.
  - In `guest.test.ts`:
    - the `pwd; echo $HOME` case (`:74`) expects `GUEST_HOME`;
    - add `"/surogate"` to the root's own mounts set (`:615`) if the mount listing shows it;
    - add a test `the user's profile path above the chat's folder takes no new folder`. It runs `mkdir "$(dirname "$PWD")/Desktop" 2>&1; echo rc=$?` and `ls -d ~/Desktop 2>&1; echo rc=$?`, and expects `Permission denied` with `rc=1`, then `No such file or directory` with `rc=2`.
- [ ] **Step 2: Run** `cd desktop && npm run build && npx vitest run test/host-user.test.ts`. Expected: FAIL (home is the profile path).
- [ ] **Step 3: Implement.** `own()` and `windows()` return `home: GUEST_HOME`. Update the header comment and the file's doc line to say the home is the root's private home at a fixed path, never the user's profile. `enter-root`, `guest/root.ts` and `rootEnvironment` need no change: they take `user.home` as given. *Ruling for the builder's open point:* every root moves, existing chats included. Tools installed in an old home with absolute paths must be reinstalled. Say so in the PR description.
- [ ] **Step 4: Run** `npx vitest run test/host-user.test.ts test/guest-root.test.ts` (expected: PASS), then on the laptop with KVM `npx vitest run test/vm/guest.test.ts` (expected: PASS, the new test included).
- [ ] **Step 5: Commit** `fix(desktop): give a chat's private home a fixed path, not the user's profile`.

---

## Part A

### Task 3: Journal: read grants and copy records

**Files:**
- Modify: `desktop/src/journal/journal.ts:82` (tables), `desktop/src/journal/bindings.ts` (accessors, `retire`)
- Test: `desktop/test/journal.test.ts`

**Interfaces:**
- Produces, on `Bindings`:
  - `grantRead(root: string, folder: string): void`
  - `takeBackRead(root: string, folder: string): void`
  - `reads(root: string): string[]` (guest paths, in grant order)
  - `recordCopy(root: string, copy: string, original: string, revision: string): void`
  - `copyOf(root: string, copy: string): { original: string; revision: string } | undefined`
  - `forgetCopy(root: string, copy: string): void`
- Tables:
  - `read_grants (root TEXT NOT NULL, folder TEXT NOT NULL, PRIMARY KEY (root, folder))`
  - `outside_copies (root TEXT NOT NULL, copy TEXT NOT NULL, original TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY (root, copy))`
- Pattern: mirror `allowDomain`/`disallowDomain`/`domains` (`bindings.ts:160-175`). Both writes call `this.changed(root)`. `retire()` (`:99`) deletes both tables' rows for the root; `replace()` keeps them.

- [ ] **Step 1: Write failing tests** in `journal.test.ts`, under "the bindings":
  - `keeps a chat's read grants until taken back or the chat retires`: grant two, `reads` returns both in order; take back one; retire empties.
  - `grants nothing to a root it does not bind`: `INSERT … SELECT … FROM bindings`, as domains does.
  - `records a working copy's original and revision, and forgets it`.
  - `tells its watchers when a grant changes`.
- [ ] **Step 2: Run** `npx vitest run test/journal.test.ts`. Expected: FAIL (methods missing).
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): keep a chat's read grants and its working copies' originals`.

### Task 4: Outside paths: known folders, grantable folders, containment

**Files:**
- Create: `desktop/src/outside/places.ts`
- Test: `desktop/test/outside-places.test.ts`

**Interfaces:**
- Produces:
  - `KNOWN_FOLDERS: readonly ["Desktop", "Downloads", "Documents", "Pictures"]`
  - `knownFolder(name: string, getPath: (name: "desktop"|"downloads"|"documents"|"pictures") => string): string | null`, case-insensitive, returning the **guest** path (`guestPathOf`). Main passes `app.getPath`, so OneDrive-redirected Desktops resolve.
  - `grantable(folder: string, guards: FolderGuards): { ok: true; folder: string } | { ok: false; message: string }`, which wraps `checkFolder` and also refuses the chat's own folder ("That is this chat's folder already.").
  - `withinGrant(grants: string[], path: string, fd: FdPaths): string | null`: the granted root holding `path` when the real path, walked with `fd` with no link followed out, stays inside it; otherwise `null`.
- Consumes: `checkFolder`, `FolderGuards` (`desktop/src/binding/folder.ts:35,130`); `guestPathOf` (`desktop/src/session-path.ts`); `fdPaths` (`desktop/src/files/fd-paths.ts:54`).

- [ ] **Step 1: Write failing tests** (temporary folders; on Windows a `-win32` sibling test only if `fd-paths-win32` needs it, following `fd-paths.test.ts`):
  - `resolves known folders through the OS's own paths, whatever case the agent writes`: a fake `getPath` maps desktop to `/home/a/OneDrive/Desktop`, and `knownFolder("desktop", …)` returns it.
  - `refuses as a grant every folder checkFolder refuses`: home, a folder holding home, `~/.ssh`, the app's data folder.
  - `refuses a grant of the chat's own folder`.
  - `finds the grant that holds a path`: nested files, the deepest-first grant.
  - `refuses a path that leads out of the grant through a link`: a symlink inside the grant to `/tmp/elsewhere` gives `null` (Review Focus 1).
- [ ] **Step 2: Run** `npx vitest run test/outside-places.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): name, check and contain folders outside a chat's folder`.

### Task 5: Prompt window: checkbox list and the Recycle Bin style

**Files:**
- Modify:
  - `desktop/src/shell/prompt-content.ts:12-46` (types)
  - `desktop/src/shell/pages/prompt.ts` (`Content`, `draw()`, `answer()`)
  - `desktop/src/shell/pages/prompt.html` (a `#prompt-checks` fieldset)
  - `desktop/src/shell/shell.css` (`.prompt.danger`)
  - `desktop/src/shell/pages-preload.cts:108`
  - `desktop/src/shell/prompt-window.ts:40,147`
- Test: `desktop/test/prompt-content.test.ts`; a prompt-window test next to the existing one, if there is one (`grep -l openPrompt desktop/test`).

**Interfaces:**
- Produces:
  - `PromptContent.checks: { legend: string; items: Array<{ value: string; label: string; detail: string }> } | null`. Every item starts ticked.
  - `PromptContent.danger: boolean`: the page sets class `danger` on `.prompt`.
  - `PromptAnswer.checked: string[]`.
  - The `prompt:answer` IPC takes `(button, choice, checked)`. `prompt-window.ts` keeps only values present in `content.checks.items`, and an allowing button with nothing ticked answers like Deny.

- [ ] **Step 1: Write failing tests:**
  - `returns the ticked items with the answer, and nothing it did not show`;
  - `answers an allow with nothing ticked as a denial`;
  - existing content keeps `checks: null`, `danger: false`.
- [ ] **Step 2: Run** `npx vitest run test/prompt-content.test.ts` (plus the window test). Expected: FAIL.
- [ ] **Step 3: Implement.** Extend every existing content builder with `checks: null, danger: false`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): let a prompt carry a list to tick and a warning style`.

### Task 6: The four outside windows' content

**Files:**
- Modify: `desktop/src/binding/approvals.ts:74-97` (`ApprovalRequest` variants), `desktop/src/shell/prompt-content.ts` (`approval()` cases)
- Test: `desktop/test/prompt-content.test.ts`

**Interfaces:**
- Produces `ApprovalRequest` variants:
  - `{ kind: "read_folder"; chat: ChatLabel; folder: string }`
  - `{ kind: "copy"; chat: ChatLabel; to: string; files: Array<{ from: string; size: number }> }`
  - `{ kind: "trash"; chat: ChatLabel; files: string[] }`
  - `{ kind: "put_back"; chat: ChatLabel; copy: string; original: string }`
- Exact text (paths through `shownPath`):

| kind | title | lead | buttons (id: label) | extras |
|---|---|---|---|---|
| `read_folder` | `Let {agent} read a folder?` | `{agent} asks to read {folder} and its subfolders for this chat. It can list and read files there, and change nothing.` | `deny: Deny`, `allow: Allow for this chat` | none |
| `copy` | `Copy {n} file(s)?` | `{agent} asks to copy these files to {to}. A file whose name is taken there is saved with a number added; nothing is overwritten.` | `deny: Deny`, `allow: Copy the ticked files` | `checks`: one item per file, `label` the name, `detail` `{from folder} · {size}` |
| `trash` | `Move to the Recycle Bin?` | `{n} file(s) will be removed from where they are and moved to the Recycle Bin, where you can restore them.` | `keep: Keep them`, `trash: Move to the Recycle Bin` | `danger: true`, `checks` one per file, focus on `keep` |
| `put_back` | `Put the changes back?` | `The changes {agent} made in its copy go into {original}. The old version goes to the Recycle Bin, and the working copy in the chat's folder is deleted.` | `deny: No, keep only the copy`, `allow: Put them back` | `danger: true` |

  On macOS the word is "Trash" instead of "Recycle Bin", with `forThisOs`, as the app already does elsewhere.
- No variant offers `stop_asking`.

- [ ] **Step 1: Write failing tests:** one per kind, asserting title, lead, button ids, focus `deny`/`keep`, `danger`, and the items.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): word the windows for reading, copying, trashing and putting back`.

### Task 7: Admitting `outside.*` operations

**Files:**
- Modify:
  - `desktop/src/binding/approvals.ts:293` (`admit`)
  - `desktop/src/binding/approvals.ts:109` (`ApprovalPrompts.approve` returns `{ answer: ApprovalAnswer | "trash" | "keep"; checked: string[] }`)
  - `desktop/src/shell/prompts.ts:85`
- Test: `desktop/test/approvals.test.ts`

**Interfaces:**
- Kinds and args (guest paths):
  - read kinds, never asked once the path is within a grant: `outside.resolve {path}`, `outside.stat {key}`, `outside.read {key, max_bytes}`, `outside.read_lines {key, encoding, offset, limit, max_bytes}`, `outside.list_dir {key}`, `outside.walk {key, skip, skip_top, skip_hidden, since}`
  - asked: `outside.grant {folder}`, `outside.copy {files: string[], to: string}`, `outside.trash {files: string[]}`, `outside.put_back {copy, original}`
- `admit()` branches on `kind.startsWith("outside.")` **before** `UNASKED` and `asking()`. In order:
  1. A thread is refused with the Global Constraints message (type `"denied"`).
  2. Read kinds pass (the layer checks the grant).
  3. `outside.grant` asks `read_folder` and, on `allow`, calls `bindings.grantRead`.
  4. `outside.copy` asks `copy`; on allow it stores the ticked list for the operation.
  5. `outside.trash` asks `trash`; only `"trash"` admits, with its ticked files.
  6. `outside.put_back` asks `put_back`.
  7. A denial is `{ error: { type: "denied", message: "The user said no." } }`.
- Produces `Approvals.chosenFor(operationId: string): string[] | undefined`, read by the layer in Task 8. It's cleared once read.

- [ ] **Step 1: Write failing tests,** with the `User` fake from `approvals.test.ts:30` extended to return `checked`:
  - `asks for outside kinds even when the chat works freely` (Review Focus 5);
  - `refuses every outside kind in a project's thread without asking`;
  - `records a read grant only when allowed`;
  - `admits only the ticked files of a copy`;
  - `admits a trash only on its own button`;
  - `never offers stop asking for an outside window`.
- [ ] **Step 2: Run** `npx vitest run test/approvals.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS, with the existing approvals tests unchanged.
- [ ] **Step 5: Commit** `feat(desktop): ask for outside-folder operations, free mode or not`.

### Task 8: The `Outside` layer: grants and reads in the main process

**Files:**
- Create: `desktop/src/outside/executor.ts`
- Test: `desktop/test/outside-executor.test.ts`

**Interfaces:**
- Produces: `class Outside implements ToolLayer` (`desktop/src/shell/device-stack.ts:31`):

  ```ts
  constructor(options: {
    tools: ToolLayer;
    bindings: Bindings;
    approvals: Pick<Approvals, "chosenFor">;
    fd: FdPaths;
    getPath: (name: "desktop" | "downloads" | "documents" | "pictures") => string;
  })
  ```

  - `run(op, signal)` handles `outside.*` and passes everything else to `tools`.
  - `refusal(op)` refuses before any window:
    - an `outside.grant` whose folder `grantable` refuses, or that names neither a known folder nor an absolute path;
    - read or `copy`/`trash` paths with no grant holding them (`withinGrant` returns null): message `"No permission to read {folder}: ask for it first with request_folder_access."`.
  - It delegates every other `ToolLayer` member to `tools`.
- Read kinds mirror the file helper's outcomes (`desktop/src/files/operations.ts` `KINDS`), so the server's `answered()` maps them the same way:
  - `outside.resolve` gives `{ok: key}`, where key is the real guest path;
  - `outside.read` gives `{ok: base64}` up to `max_bytes`, or a transfer exactly as `read` does (add `"outside.read"` to `RESULT_TRANSFERS` in `desktop/src/operations/runner.ts`);
  - `outside.walk` gives `{files, truncated, cursor}`;
  - `outside.stat` gives the revision via `revisionOf` (`desktop/src/files/operations.ts:151`).
- `outside.grant` answers `{ok: {folder: <guest path granted>}}`.

- [ ] **Step 1: Write failing tests,** with a fake inner `ToolLayer`, a real journal and real temp folders:
  - `grants a known folder by its OS path and answers where`;
  - `refuses reading a folder it was never granted, before anyone is asked`;
  - `reads, lists and walks inside a grant like the folder's own tools`: the same outcome shapes as `perform()`;
  - `refuses a path that leads out of the grant through a link`;
  - `passes every other kind to the tools below`.
- [ ] **Step 2: Run** `npx vitest run test/outside-executor.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): read granted folders outside a chat's folder from the app itself`.

### Task 9: `outside.copy`

**Files:**
- Modify: `desktop/src/outside/executor.ts`
- Test: `desktop/test/outside-executor.test.ts`

**Interfaces:**
- `outside.copy {files, to}` copies the files `approvals.chosenFor(op.id)` returns:
  - `to` is the chat's folder or a path inside a grant;
  - each source is in the chat's folder or inside a grant.
- **Into the chat's folder:** written through the folder's own tools, as `downloads.ts` `save()` does (`resolve`, `stat`, `write {create: true}` run on `tools`, already admitted). The layer records `bindings.recordCopy(root, copyKey, original, revisionOfOriginal)` for each.
- **Elsewhere:** written by `fd` with exclusive create, then checked by size and sha256 (`hashOf`, `desktop/src/download.ts:48`).
- **A taken name:** `name (1).ext`, `name (2).ext`, … up to 100 (a helper `numberedName(name: string, n: number): string`).
- **Outcome:** `{ok: {copied: Array<{from, to}>, skipped: Array<{from, reason}>}}`.

- [ ] **Step 1: Write failing tests:**
  - `copies the ticked files into the chat's folder and remembers their originals`;
  - `copies from one granted folder into another, and never through the chat's folder`;
  - `numbers a copy whose name is taken instead of overwriting` (Review Focus 4);
  - `refuses a destination that is neither the chat's folder nor granted`;
  - `reports a file that vanished before the copy as skipped, and copies the rest`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): copy files in, out and between granted folders without overwriting`.

### Task 10: `outside.trash` and `outside.put_back`

**Files:**
- Modify: `desktop/src/outside/executor.ts`, plus a `trash` function in its options (main passes `shell.trashItem` after `hostPathOf`)
- Test: `desktop/test/outside-executor.test.ts`

**Interfaces:**
- `outside.trash {files}`: each ticked file must be inside a grant; `trash(hostPathOf(file))` for each. Outcome: `{ok: {trashed: string[], kept: string[]}}`.
- `outside.put_back {copy, original}`, in order:
  1. `copyOf(root, copy)` must name `original`.
  2. The original's current revision must equal the recorded one; otherwise `{error: {type: "conflict", message: "{original} changed since it was copied. Nothing was replaced; the changes are in {copy}."}}`.
  3. Read the copy through `tools` (`read` on the chat's folder).
  4. Write it beside the original as a temporary file by exclusive create.
  5. `trash(original)`.
  6. Rename the temporary file to the original's name.
  7. Check size and sha256.
  8. `delete` the copy through `tools`, then `forgetCopy`.
  - If 4 or 5 fails, remove the temporary file and answer an `os` error naming the cause. The original and the copy stay.
  - If 6 or 7 fails after the trash, answer `{error: {type: "os", message: "The old version is in the Recycle Bin; the changes are still in {copy}."}}` and keep the copy.
- Outcome: `{ok: {original, copyDeleted: true}}`.

- [ ] **Step 1: Write failing tests,** with a fake `trash` that moves files to a temporary "bin" folder:
  - `moves only the ticked files to the bin`;
  - `puts the changes back, sends the old version to the bin and deletes the working copy`;
  - `refuses to put back when the original changed since it was copied` (Review Focus 2);
  - `leaves everything as it was when the original cannot be replaced` (fake trash throws `EBUSY`; Review Focus 3);
  - `refuses a put back for a copy it did not make`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(desktop): move originals to the Recycle Bin and put edited copies back`.

### Task 11: Wiring, the Settings row, and the capability flag

**Files:**
- Modify:
  - `desktop/src/shell/main.ts:993` (wrap the tool stack in `Outside`, passing `app.getPath`, `fdPaths`, and `shell.trashItem` after `hostPathOf`)
  - `desktop/src/shell/main.ts:2565` (`settings:take-back-read` handler)
  - `desktop/src/shell/pages-preload.cts:79`
  - `desktop/src/shell/folders.ts:13-21,57` (`ChatRow.reads: string[]`)
  - `desktop/src/shell/pages/settings.ts:32,152` (row `Reads {folder}` with "Take back")
  - `desktop/src/link/protocol.ts:196` (hello `outside: true`)
  - `desktop/src/link/client.ts:54,171`
  - `desktop/src/device.ts:25,53`
  - `desktop/src/shell/device-stack.ts:211,243`
- Test: `desktop/test/folders.test.ts`, `desktop/test/device-stack.test.ts`, `desktop/test/client.test.ts` (or wherever hello is tested: `grep -l "copies: true" desktop/test`)

**Interfaces:**
- Hello carries `outside: true` whenever the stack has the `Outside` layer (as `copies` does).

- [ ] **Step 1: Write failing tests:**
  - `lists a chat's read grants with a way to take each back`;
  - `says it can work outside a chat's folder in its hello`;
  - `runs an outside kind through the Outside layer and others below it`.
- [ ] **Step 2: Run** those files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them, then `npm run typecheck`. Expected: PASS, no type errors.
- [ ] **Step 5: Commit** `feat(desktop): wire outside-folder work into the app and its settings`.

### Task 12: Server: link capability, read routing, and the four tools

**Files:**
- Modify:
  - `surogates/devices/link.py:316,362-368,682,770-790` (`_Link.outside`; `deliver` completes `outside.*` for an app without it)
  - `surogates/devices/workspace.py` (docstring kinds table; `DeviceWorkspaceIO` routing; `RESULT_TRANSFERS += {"outside.read"}`)
  - `surogates/tools/router.py:46` (`TOOL_LOCATIONS`)
  - `surogates/tools/runtime.py:50-130`
  - `surogates/harness/tool_schemas.py:124-234` + `surogates/harness/loop.py:1898` (`on_device` flag)
  - `surogates/harness/tool_exec.py:645` (`THREAD_REFUSED_TOOLS`)
  - `surogates/harness/prompts/identity/workspace_rules_device.md`
  - `surogates/tools/builtin/file_ops.py` (`search_files` outside the folder answers the Global Constraints sentence)
- Create: `surogates/tools/builtin/outside_folder.py`
- Test: `tests/test_device_link_delivery.py`, `tests/tools/test_device_workspace_io.py`, a new `tests/test_outside_folder_tools.py` (on the `Laptop` fake from `tests/test_device_browser_tools.py`)

**Interfaces:**
- `NO_OUTSIDE_OUTCOME = {"error": {"type": "unsupported", "message": "The Surogate app on this computer cannot work outside the chat's folder yet. Update it, then try again."}}`
- **Routing in `DeviceWorkspaceIO`:**
  - `resolve(path)`: when `path` is absolute and not under `root`, call `outside.resolve` and remember the key in `self._outside`;
  - `stat`, `read`, `read_lines`, `list_dir`, `walk` with a remembered key use the `outside.*` kind;
  - `ripgrep`, `write`, `delete` with one raise `WorkspaceSandboxError("Files outside the chat's folder are read-only: use copy_files, move_to_trash or put_back.")`.
- **Tools** (`toolset="file"`, HARNESS, offered only when `on_device`, refused in threads). Each answers the device outcome as JSON, or the refusal text, like `answering_refusals` (`surogates/devices/browser.py:180`):
  - `request_folder_access(folder: str)`: `"Desktop"`, `"Downloads"`, `"Documents"`, `"Pictures"`, or an absolute path. Kind `outside.grant`.
  - `copy_files(files: list[str], to: str)`: absolute paths, or paths relative to the chat's folder; `to` is `"."` for the chat's folder. Kind `outside.copy`.
  - `move_to_trash(files: list[str])`: kind `outside.trash`.
  - `put_back(working_copy: str, original: str)`: kind `outside.put_back`.
- **`workspace_rules_device.md` gets a second paragraph:**

```
Files outside the chat's folder: ask once with request_folder_access (Desktop, Downloads, Documents, Pictures, or a path the user names); then list_files and read_file work there with absolute paths, read-only. To change a file there, copy it into the chat's folder with copy_files, change the copy, then offer put_back. To move a file, copy it (or upload it) and then move_to_trash the original. Each of these shows the user a window from the app; never ask for the same thing in the chat instead. After working on a copy, say where the changed file is: put back in its place, or only in the chat's folder with its full path.
```

- [ ] **Step 1: Write failing tests:**
  - `completes an outside kind for an app that cannot do it, without sending it`;
  - `routes reads of a path outside the folder to outside kinds`;
  - `refuses writes, deletes and searches by content outside the folder`;
  - `offers the outside tools only in a device chat`;
  - `refuses the outside tools in a project's thread`;
  - one test per tool asserting the kind and args sent and the answer returned.
- [ ] **Step 2: Run** each file with `just test <file> -n0`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** each file. Expected: PASS. Then run `tests/test_prompt_workspace_block.py`, `tests/test_device_browser_tools.py` and `tests/test_device_tool_exec.py` to check nothing nearby broke.
- [ ] **Step 5: Commit** `feat(devices): read, copy, trash and put back files outside a desktop chat's folder`.

### Task 13: Live check (Ubuntu, then Windows)

No code. This needs Monica at the screen. The results go into the PR description.

- [ ] **Step 1: Separate data.** Check whether a source build shares the installed app's data folder: `app.setPath("userData")` in `desktop/src/shell/main.ts`, and the VM disks under `~/.local/share/surogate/vm`. If it does, start the source build with its own data folder (a `SUROGATE_DATA`-style override if one exists; otherwise ask Monica before testing), and quit the installed app.
- [ ] **Step 2: Ubuntu.** Local stack from this worktree's server code (`start-local.sh` can't target a worktree: run the worker and API from this worktree as `Misc/local-dev-notes.md` describes), a dev agent of Monica's own, and the desktop app from source (`cd desktop && npm start`). Scenarios:
  1. "uită-te pe Desktop după facturi": one read window, a list in the chat.
  2. Move a file from `Desktop/Scan` to `Desktop/PONTAJE`: one copy window, one Recycle Bin window, nothing left in the chat's folder.
  3. Add 2 rows to an Excel file on the Desktop: copy in, edit, put-back window; the file is on the Desktop, the old version in the Trash, no working copy left.
  4. The same with the Excel file open in LibreOffice: put-back refused or done safely, copy kept.
  5. A chat in free mode: windows still show.
  6. `mkdir ~/Desktop` in the terminal fails; the agent doesn't claim a Desktop file.
- [ ] **Step 3: Windows VM `win11`** with a built installer: scenarios 1-5, with the file open in Excel for 4, and a Desktop redirected to OneDrive if the VM has one.
- [ ] **Step 4:** Record the results, then run `/simplify` and `/code-review` on the branch before opening the PR.
