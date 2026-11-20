# Design: Surogate Desktop, working with files outside the chat's folder

Date: 2026-11-18. Status: draft for review. Repo: `surogates` (server in `surogates/`, desktop app in `desktop/`). All code references are to `master` at `c195f8cc4` (2026-11-17).

Two parts:

- **Part 0:** stop the agent treating its private home as the user's profile.
- **Part A:** let the agent read, copy and (with clear approval) move files outside the chat's folder.

Part 0 is small and ships first; Part A builds on it.

## Why

A desktop chat works in one folder, chosen when the chat starts. Real colleagues keep their files on the Desktop and in Downloads, and the chat's folder is often not the one that holds the file they mean.

- **Diana, 2026-11-18 (prod sessions `4919ff77…`, `f21e7763…`).** "Ai acces la folderul Scan?" and "ia cererea de avans din Scan de pe Desktop și mut-o în PONTAJE PENTRU STATE". The chat's folder was `Pictures\Scans`, which held neither file. The agent could only say "I can't reach the Desktop"; it searched the cloud for 7 minutes instead, and she gave up ("o voi muta eu").
- **Files 7 and 8 (`bugs-found.md`).** The agent "made a folder on the Desktop" and "saved a file in the profile". Both landed in a private per-chat home the user never sees, and the agent said they were done.

**What success looks like** (Diana's move from `Desktop\Scan` to `Desktop\PONTAJE` is: one read grant for the Desktop, one copy window, one Recycle Bin window): Diana says "uită-te pe Desktop după facturi" or "mută cererea de avans din Scan în PONTAJE". The agent asks once to read the Desktop, finds the files, does the job with one or two clear approvals, and never leaves her guessing where a file is.

## Goals

- The agent never presents its private home as the user's Desktop, Downloads or profile.
- After one approval per folder, the agent can list, search and read files in a folder outside the chat's folder.
- The agent can copy files into the chat's folder, or out of it to another folder, with one approval per batch.
- A "move" out of a place outside the chat's folder ends with the original in the Recycle Bin, behind its own clear approval.
- When the agent edits a copy of a file the user asked to change, the change goes back into the original (with approval), or the agent says plainly that only the copy changed and where it is.

## Non-goals

- **Several folders per chat, or changing a chat's folder.** This was considered (option C in the investigation) and left for later.
- **The terminal outside the chat's folder.** Commands keep running only in the chat's folder.
- **Editing files in place outside the folder.** All changes happen on a copy in the chat's folder and are put back.
- **The home folder itself, or credential folders.** These stay refused as today.
- **Cloud chats.** Nothing changes there.
- **Project threads that work in their own copy of the folder** (the "landing" flow: `surogates/devices/binding.py:10`, `desktop/src/files/land.ts`, `binding/approvals.ts:296`). In this version the Part A tools are refused in such threads with a plain message ("do this from the main chat"); grants belong to the root chat. *My call:* a landing writes into the real folder without asking again, so a file approved and checked here could be overwritten later by an unapproved landing. Supporting threads needs its own design against the desktop spec's Section 13.

## Anti-goals: what would make this a failure even if it works

- The agent can change or delete a user's file without a window from the app naming that file.
- A permanent delete: every removal of a user's original goes to the Recycle Bin.
- A user sees a "done" while the change exists only in a copy, without being told where.
- Copies piling up in the chat's folder after a successful move or put-back.
- An approval that can be faked by the agent's own chat text: every approval for outside files is the app's own window.

## Constraints

- **The app's existing safety rules stay in place:**
  - folders refused as a chat's folder are refused here too (`desktop/src/binding/folder.ts:130-187`, `checkFolder`);
  - "reads are never asked about" stays true **inside** the chat's folder only.
- **Windows first.** Colleagues use Windows; Linux and macOS must keep working. Windows' Desktop is often redirected to `OneDrive\Desktop`, so "Desktop" must be the real one, as Windows reports it (Electron `app.getPath("desktop")`).
- **Free mode** ("allow and stop asking for this chat") must **not** cover anything outside the folder. Every Part A approval is asked even in free mode (my call: free mode was granted for one folder the user chose, not for the rest of the computer).
- **No em dashes in user-facing text.** Prompts in Romanian when the app is in Romanian, following the existing prompt text in `desktop/src/shell/prompt-content.ts`.

## Approaches considered

1. **The app handles outside files itself (chosen).**
   - The file tools learn a second kind of place: a granted folder, read-only.
   - Copying, putting back and moving to the Recycle Bin are new operations the desktop app runs in its main process, each behind an approval window.
   - **Pros:** no change to the VM or its shares; it reuses the app's approval windows, its per-chat grants (like allowed hosts) and its path checks.
   - **Cons:** the terminal can't reach granted folders, so processing needs a copy first. That is accepted, and is also what keeps the originals safe.
2. **Mount granted folders into the VM, read-only.** The terminal could then read them too.
   - **Rejected:** a running chat can't get a new mount today (a changed folder tears the chat's root down: `desktop/src/vm/manager.ts:470-475`), there is a cap of 8 shares across all chats (`MAX_SHARES`), and it is a much bigger change.
3. **File picker only:** the user picks each file in a Windows dialog.
   - **Rejected:** the agent can't search, so "look on the Desktop for facturi" doesn't work.
4. **The agent names an exact path and the user approves each file.**
   - **Rejected:** the agent can't know paths it can't list, and it means one window per file.

## Part 0: the private home

**Today:**
- Each chat's root gets an empty home on `sessions.img`, mounted at the user's real profile path: `/mnt/c/Users/<name>` on Windows (`desktop/vm/enter-root:75-84`, `desktop/src/host-user.ts`).
- The agent's instructions say "Only `$HOME` (your working directory) is writable" (`surogates/harness/prompts/identity/workspace_rules.md`, used from `surogates/harness/prompt.py:956-959`). That is true in cloud chats and false on a computer.
- `surogates/office/agent.py:63-75` already knows that on a computer `~` is not the folder.

**Change:**

1. **Instructions (server).** Device chats get their own workspace rules, in place of the current block when `execution.kind == "device"`. They must say:
   - your working folder is the chat's folder, named by its path as the user sees it (`C:\Users\…\Scans` on Windows, from `desktop/src/session-path.ts`'s mapping or the existing Windows-path logic the soul already describes);
   - give file tools paths relative to it;
   - `~` and `$HOME` are a private scratch area of this chat that the user cannot see, and are not the user's Desktop, Downloads or profile;
   - never tell the user a file is somewhere they can open unless it is in the chat's folder, a granted folder (Part A), OneDrive/SharePoint, or Outlook.
2. **Mount point (desktop).** Mount the private home at a neutral path that is never a real user path on any OS (for example `/surogate/home`), instead of the user's profile path. Checked in the code (master `c195f8cc4`, 2026-11-18); nothing depends on HOME being at the profile path:
   - **Single source:** the home path comes from one place, `hostUser().home` (`desktop/src/host-user.ts`; Windows: `guestPathOf(homedir())`, Linux and macOS: the real `homedir()`). It is passed to `vm/manager.ts` (`user`), to `guest/root.ts` (`place.home` at `:910`, checked at `:502`, given to `enter-root` at `:551`) and to `rootEnvironment` (`:126-142`: `HOME`, and the cloud layout's `/home/sandbox` paths in `/etc/surogate/environment` rewritten to it). `enter-root` uses it for the bind mount (`:79-81`) and the `/etc/passwd` entry (`:88`). All of these follow the new value with no other change.
   - **Unaffected:**
     - commands given the workdir `~` or `$HOME` already run in the chat's folder (`guest/command.ts:20-21,100`);
     - browser downloads go to `Downloads` inside the chat's folder (`browser/downloads.ts:41,216`), not HOME;
     - copy makers use `HOME=/nonexistent` (`guest/places.ts:77`);
     - the host-side file helper uses the app's own host HOME for its guards (`hosts/host.ts:172-251`, `hosts/start.ts:356`), a separate thing.
   - **What the change gives:** the chat's folder stays mounted at its real path. Its parent folders (`/mnt/c/Users/<name>/…` on Windows, `/home/<name>/…` on Linux) are then made by `mkdir -p` as root in the root's overlay (a tmpfs, `enter-root:27-41`), so they are owned by root and the agent's user can't write in them. `~/Desktop` stops existing, and `mkdir /mnt/c/Users/<name>/Desktop` fails with "Permission denied" instead of making a private copy. The builder must confirm this with a test (`desktop/test/`), on Windows and on Linux.
   - **Linux and macOS have the same bug** (the private home sits at `/home/<name>` or `/Users/<name>`), so the change applies to all three.
   - **One real cost: chats that already exist.** A chat's home persists on `sessions.img`. Tools the agent installed there (for example `uv tool`, `pipx`, user-level npm) can hold absolute paths to the old home and would break after the move. *Builder's call:* keep the old path for roots that already have a home (record the path in the root's data folder at first setup), or accept that those tools must be reinstalled. New chats are unaffected.

**Done when:**
- In a desktop chat on Windows, "pune fișierul pe Desktop" gets "pot lucra doar în folderul acestui chat (…)", or with Part A the approved copy-out.
- `ls ~/Desktop` in the terminal shows no folder that looks like the user's, and `mkdir` under the user's profile path fails.
- Bugs Files 7 and 8 can be closed.

## Part A: outside the folder

### Behaviour (agreed with Monica)

**1. Reading a folder: one approval per folder, for the chat.**
- The agent asks to read a folder, for example "Desktop". The app's window: "Agentul vrea să citească folderul **Desktop** (`C:\Users\Diana\Desktop`), doar citire, pentru acest chat. Permiți?" Buttons: Allow / Deny.
- After Allow, the file tools can list, search and read anything under that folder (subfolders included), but cannot write, rename or delete there.
- The grant lasts for the chat. It shows in the chat's settings next to the allowed hosts, where the user can remove it.
- **Known names:** Desktop, Downloads, Documents, Pictures, resolved by the app from the OS (`app.getPath`), OneDrive redirection included. The agent may also name a full path, for example a folder the user mentions.
- A folder refused by `checkFolder` (the home itself, a folder holding it, credential and app folders, network volumes) is refused without a window, with a plain message the agent passes on.

**2. Copying in or out: one approval per batch, with checkboxes.**
- **Copying in:** into the chat's folder, to work on files with the terminal or to edit a copy. The app's window lists each file, with a checkbox, from where to where, and its size. The user can untick files. Allow copies the ticked ones; Deny copies none.
  - To add a file the agent didn't list, the user says so in the chat; the agent searches again and shows a new window.
  - Nothing goes into the chat's folder when the agent only needs to **read** a file, for example to upload it to SharePoint: it reads it where it is.
- **Copying out, or between two outside folders:** from the chat's folder, or straight from one granted folder to another (for example `Desktop\Scan` to `Desktop\PONTAJE PENTRU STATE`), same window. Nothing passes through the chat's folder. **The destination must be inside a granted folder** (decided with Monica, 2026-11-18): a read grant is what makes a folder a possible destination; each copy into it still needs its window, and only adds new files (never overwrites). The app checks this itself; it never sees the chat text, so "a folder the user named" can't be the rule.
- **Name collisions.** A copy never overwrites: if the name exists at the destination, the window says so and the copy gets a suffix (" (1)"). Replacing a file is only the put-back step below.

**3. Moving: copy, then a separate Recycle Bin window.**
- A move is a copy (or an upload, for example to SharePoint) followed by removing the original. The removal is its **own window, every time**, in a different style from the approval windows, with no "for this chat" option. For example: "Fișierul **cerere avans.pdf** va fi **șters de pe Desktop**. Îl găsești în Coșul de gunoi." Buttons: "Șterge" / "Păstrează".
- The original goes to the Recycle Bin (`shell.trashItem`), never a permanent delete.
- "Păstrează" turns the move into a copy, and the agent says so.
- The removal happens only after the copy or upload is checked (size, and hash for local copies).

**4. Putting changes back after editing a copy.**
- When the user asked to **change** a file outside the folder: the agent copies it in (step 2), edits the copy, shows what changed in the chat, then asks to put it back. The app's window: "Pun modificările în **Registru.xlsx** de pe Desktop? Versiunea veche merge în Coșul de gunoi; copia de lucru din folderul chatului se șterge."
- **Yes:**
  - the app checks that the original hasn't changed since it was copied (its revision, as the app already compares for writes in the folder);
  - it moves the old version to the Recycle Bin, puts the new one in its place, checks it, then removes the working copy from the chat's folder;
  - if any step fails (the original changed, the file is open in Excel and locked, the disk refuses), nothing is replaced, the working copy stays, and the agent says why and where the copy is.
- **No:** the working copy stays, and the agent says: "Modificările sunt doar în copia din folderul chatului, `C:\…\Registru.xlsx`. Fișierul de pe Desktop a rămas neschimbat."
- The agent offers this step whenever it edited a copy of a file the user asked to have changed, and not for files copied in only as sources.

**5. What the agent says at the end.** Whenever it worked on a copy, the final message says where the changed file is: put back in the original place, or only in the chat's folder, with the full path. This is a soul or skill rule, plus training examples; see "Agent behaviour" below.

**What is not allowed outside the folder at all:** writing in place, renaming in place, deleting except through step 3 or step 4, and the terminal.

### Agent-facing tools (proposal; final names are the builder's)

| Tool | Does | Asks |
|---|---|---|
| `request_folder_access(folder)` | grants read access to a known name or a path | window 1, unless already granted in this chat |
| existing `read_file`, `list_files`, `search_files`, `office_read` | accept absolute paths inside a granted folder | no (read-only) |
| `copy_files(files, to)` | copies a list into the chat's folder, out of it, or between two granted folders; the destination must be the chat's folder or inside a granted folder | window 2 (one per call) |
| `move_to_trash(files)` | moves originals outside the folder to the Recycle Bin, only as the second half of a move | window 3 (one per call, every time) |
| `put_back(working_copy, original)` | replaces the original with the edited copy, as in step 4 | window 4 |

- Writes, renames and terminal commands that name a path in a granted folder are refused with a message that points to `copy_files` / `put_back`. The model learns the rule from the refusal, not only from the instructions.
- The server keeps no record of grants: the desktop app is the authority, as it is for the chat's folder today. The worker only forwards operations, as `DeviceWorkspaceIO` does (`surogates/devices/workspace.py`).

### Where it lives (for the plan)

**Desktop app (`desktop/src/`):**
- **Grants:** a table in the bindings journal (`journal/bindings.ts`), per root, like the allowed hosts at `:60`, shown in the chat's settings rows (`shell/folders.ts:13-27`) with a remove action.
- **Path resolution:** `files/paths.ts` `resolveInFolder` (`:227`) resolves a path inside a granted folder to a read-only key instead of refusing it. The traversal refusal stays for everything else. Read-only must be enforced in the app, not only by the tools.
- **File helper sandbox:** `hosts/policy.ts` `sandboxPolicy` already takes `reads?: string[]` (`:144`); granted folders go there. The helper's policy is fixed when it starts, so a new grant restarts the chat's helper or starts a second, read-only helper (builder's call).
- **New operations:** `copy_in`, `copy_out`, `trash`, `put_back`, plus `grant` for window 1. They run in the app's main process, not in the VM, and are added to the operation kinds documented in `surogates/devices/workspace.py:8-34`.
- **Approvals:** new `ApprovalRequest` kinds in `binding/approvals.ts` (`:74`); never in `UNASKED`, never covered by free mode. Text in `shell/prompt-content.ts`. The window is the existing `prompt.html` page (`shell/prompts.ts`), extended with a checkbox list for batches and a distinct style for the Recycle Bin window.
- **Recycle Bin:** Electron `shell.trashItem`.
- **Windows has no OS sandbox for the file helper** (`hosts/srt-win32.ts`, `files/fd-paths-win32.ts`): it relies on opening every path step by step from the chat's folder. So on Windows, read-only grants and every write outside the folder are protected only by the app's own checks. These operations must therefore run in the app's main process with their own path checks (refused folders, links, the granted roots), not by widening the helper; `sandboxPolicy` `reads` covers Linux and macOS only.
- **Version mismatch:** an older app that lacks these operations is told to update, following the existing pattern (the `copies` capability flag and the `KEEPS_NO_COPY` refusal).
- **Integrity:** copies checked by size and hash; put-back uses the original's revision captured at copy-in.

**Server (`surogates/`):**
- **Instructions:** the device-chat workspace rules (Part 0) and the rules for the new tools.
- **Tools:** definitions and forwarding through `DeviceWorkspaceIO` (`surogates/devices/sandbox.py:247`, `surogates/devices/workspace.py`).
- **Paths:** `surogates/office/agent.py` `relative_path` / `within_folder` must accept paths inside granted folders for reading (the desktop decides; the server should not refuse them first).
- **Logs:** `surogates/harness/tool_exec.py` `_sanitize_paths` (`:468`) must not mangle paths outside the folder in events.

**Web app:** nothing required. The chat shows tool steps as today.

### Agent behaviour (soul, skills and training, after the code)

- **Soul:** replace today's "pot lucra doar în folderul acestui chat" line with the new flow: ask to read the folder, then copy, move or put back with approval; say where changed files are.
- **Skills `gasire-documente`, `redenumire-facturi`, `instrumente-microsoft`:** local files outside the folder are reachable after a grant; uploading a local file to SharePoint reads it in place.
- **Training:** cases for "asks to read once, then searches", "edits a copy then offers put-back", "says plainly when only the copy changed", "a move is two steps".

## Error handling

- **A grant refused by the user:** the agent says it can't look there and suggests a new chat with that folder, or putting the files in the chat's folder or OneDrive.
- **A window not answered in time:** treated as Deny, as today (`timeout`).
- **A file locked by another program (Excel open):** the copy or put-back fails with a message naming the program if Windows reports it; nothing half-done.
- **A path that leaves the granted folder through a link:** refused, same rules as inside the chat's folder.
- **The computer is offline mid-operation:** existing device-operation rules apply (waits, then the chat is told).

## Testing

- **Desktop unit tests:** next to the existing ones (`desktop/test/`): path resolution with grants, read-only enforcement, a refused folder, each new operation, put-back conflict, the trash fallback.
- **Server tests:** for the tools and the instructions.
- **Live check:** on the Windows VM with the released app: Diana's two scenarios end to end, an Excel put-back while the file is open in Excel, a move kept with "Păstrează".

## Left to the builder

- Exact tool and operation names, and argument shapes.
- How the helper picks up a new grant (restart or a second read-only helper).
- Part 0: the exact neutral home path, and whether existing chats keep their old home path.
- Window layout, inside the rules above (checkboxes for batches, a distinct Recycle Bin window, Romanian text).

## Open questions

- None left for Flavius. The desktop spec the code cites is not needed for this design (Monica, 2026-11-18). Whether anything relies on HOME at the profile path was checked in the code (Part 0, step 2): nothing does, apart from tools installed in existing chats' homes.
