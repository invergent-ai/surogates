# A new chat gets its own folder by default

## Why

In Surogate Desktop a chat works in one folder on the computer, and only one chat at a time may work in a folder: the tool host takes a per-folder lock (`desktop/src/hosts/host.ts`, `desktop/src/hosts/folder-record.ts`) and keeps it until two minutes after the chat's last action (`IDLE_MS`, `desktop/src/hosts/tool-hosts.ts`).

When a new chat starts, the app offers **the last folder bound**, and only makes a fresh dated folder when there is no last folder to offer (`Binder.prepareFolder`, `desktop/src/binding/binder.ts`). So in practice every new chat lands in the folder of the chat that is already running. If that chat is busy with a long task, the new chat cannot even list a file: every tool call waits 10 seconds (`LOCK_WAIT_MS`) and fails with "This computer could not open the folder's sandbox: another chat on this computer is working in this folder; this one can use it once that one is done". Nothing told the user that the only reason is that both chats were put in the same folder.

Seen in real use: a user ran a long task in one chat, opened a second chat for a small question, and the second chat stayed blocked for its whole length. The agent spent its turn on workarounds and never did the task.

The lock itself is right: two chats running commands and writes in one folder would fight over the folder's sandbox state. The problem is that the default puts every chat in the same folder.

## Goals

1. A new chat works in **its own new folder by default**. Sharing a folder with another chat happens only when the user picks it.
2. The user can still choose **the last folder** or **any folder** with one click before sending.
3. If the folder the user confirms is in use by another chat, the user is **told on the confirmation sheet** and can switch to a new folder or browse for another.
4. When two chats do share a folder, they **really take turns**: the chat holding the folder lets go as soon as its answer is done, and the other chat **waits for it and continues on its own**, telling the user it is waiting, instead of failing every tool call after 10 seconds.

## Non-goals

- Letting two chats work in the same folder **at the same moment** (reads in parallel, per-file locks for writes). They take turns by whole answers instead. Parallel work is a separate, larger change to the tool host.
- Changing projects and their threads.
- Changing how an existing chat keeps its folder: a chat stays bound to the folder it was confirmed with (`desktop/src/journal/bindings.ts`), across days and restarts.

## Anti-goals

- The page must still not name or bind a folder before the user confirms it in the desktop app's own sheet. The confirmation after the first message is a security step (a server cannot bind a chat to a folder the user did not see) and stays.
- No empty folders left behind: a folder made for a chat and not taken is removed, as today (`removeEmpty`).

## Design

### 1. The choice under the message box (web, `web/src/features/chat/new-chat-place.tsx`)

Today: a laptop icon, the sentence "Works in a folder on this computer: the one you used last, or a new one. You confirm it when you send." (`web/src/lib/local-chat.ts`), and one link that flips between "Choose another folder" and "Use the last folder".

New:

```
[laptop] Works in [folder-plus  a new folder  v] · You confirm the folder after your first message.
```

- The laptop icon stays as today (`LaptopIcon`, lucide).
- The bracketed part is a small button showing the selected option's icon and label and a down chevron (`ChevronDownIcon`). Clicking it opens a menu that opens **upward** (the box is at the bottom of the window), with three items, each an icon, a label, a one-line hint, and a check mark on the selected one:

| Value | Icon (lucide) | Label | Hint | Button label when selected |
|---|---|---|---|---|
| `new` (default) | `FolderPlusIcon` | A new folder | Made for this chat | a new folder |
| `last` | `HistoryIcon` | The last folder | Where your last chat worked | the last folder |
| `pick` | `FolderOpenIcon` | Choose a folder… | The folder window opens after you send | a folder you choose |

- The menu names no folder: before sending, the page only chooses the kind of folder, as today.
- The trailing note reads "You confirm the folder after your first message." so users are not confused when nothing happens until they send.
- Use the UI kit's existing menu/dropdown component and the page's existing text styles; keyboard and screen-reader behaviour comes from that component.
- `new` is selected for every new chat. The choice is not remembered between chats.

### 2. Which folder the sheet offers (desktop, `desktop/src/binding/binder.ts`)

`prepareFolder`'s `choice` becomes `"new" | "last" | "pick"` (the bridge contract in `web/src/lib/desktop-bridge-contract.d.ts`, `desktop/src/shell/bridge.ts`, `desktop/src/shell/main.ts`, `web/src/lib/local-chat.ts` and `web/src/lib/local-thread.ts` carry it through):

- `new`: make a new folder with today's rules (`newFolder`: `~/Surogate/<agent>/YYYY-MM-DD`, then `YYYY-MM-DD 2`, `… 3`; the first has no number) and offer it. Removed again if not taken, as today.
- `last`: offer the last folder bound if it can still be used; otherwise make a new folder, as `"last"` does today.
- `pick`: open the folder dialog, as today.

An existing caller that still sends `"last"` keeps today's behaviour.

### 3. The confirmation sheet warns when the folder is in use (desktop, `desktop/src/shell/prompt-content.ts` `folderSheet`, `Binder.confirm`)

When the folder shown on the sheet is in use by another chat on this computer:

- Under the folder, a warning: "Another chat on this computer is working in this folder. The two chats will take turns: this chat waits while the other one is answering."
- The buttons become **Cancel · New folder · Browse… · Use this folder** (primary). "New folder" re-offers the sheet with a freshly made folder (the `new` rules); "Browse…" opens the folder dialog (what "Change…" does today).
- When the folder is not in use, the sheet and its buttons are exactly as today (**Cancel · Change… · Use this folder**). The project and thread sheets are unchanged.
- "Use this folder" on a busy folder is allowed: the chats then take turns (section 4).

"In use" means: a live tool host of another **chat** on this computer holds this folder (same device and inode), including one in its idle tail. The app already tracks its hosts per root (`ToolHosts`, `holderOf`), so this is a check of the app's own state, not a probe of the lock. A check when the sheet opens is enough. (Builder's call, made in review.)

The warning names no chat: the desktop keeps no chat titles locally, and fetching one would add a network call to the confirmation sheet. (Builder's call, made in review.)

### 4. Chats sharing a folder take turns (desktop `desktop/src/hosts/tool-hosts.ts`, `host.ts`, `folder-record.ts`; the turn-end signal in `desktop/src/shell/agent-events.ts`; the waiting notice on the server and in the web page)

The cycle the user expects: A works in the folder and B waits, saying so; A's answer is done, so B continues; while B works, A waits if asked something; and so on.

**Hand over at the end of an answer.** Today a host keeps the folder until `IDLE_MS` (2 minutes) after its chat's last operation, because the app cannot tell a finished answer from the agent thinking between two steps. New: when the holding chat's **turn ends** (the app already follows a chat's turns, `followChat` / `onTurnEnd`), its host lets go of the folder at once, **unless** it still has background processes running (`start`), in which case it keeps the folder as today until they end and the idle time passes. The 2-minute idle stop stays as the fallback for when no turn-end signal arrives.

**Hand over between answers too.** A chat's host can hold the folder with no answer under way: the page read the chat's files (`request:` reads) after its last turn ended, or the turn ended before the app followed it. Such a host would keep the folder for `IDLE_MS` against a chat that waits. New: each chat's host tracks whether its chat is **in a turn**. The turn follower (`followTurns`) tells it a turn's start (`user.message`, `coordinator.message`, `harness.wake`, `session.resume`) as well as its end. Where another chat waits for the folder, nothing of the holder runs, no process of it lives, and no turn of it is known under way, the host asks the agent the chat's status once (`restOf`, `GET /api/v1/sessions/{id}`: `completed` or `failed` is at rest, `active` is in a turn), when the waiter comes or when its own last operation ends:

- at rest: it lets the folder go at once, as at a turn's end, and the waiting chat gets it. A later read of the chat waits its turn like any other operation and then runs; the chat holding the folder by then hands it back the same way if it is at rest too;
- in a turn (`active`, or a turn start heard): it keeps the folder until that turn's end, between operations included, and asks no more until then;
- unknown (nobody signed in, the read failed, `paused`, an older agent): nothing changes, and the idle time lets the folder go.

The answer is heeded only if nothing happened since it was asked: an operation of the chat begun, or a turn's start or end heard, makes it stale, and the host asks again once it is free. The status is the agent's own truth: a message, a wake or a resume makes the chat `active` before the turn sends anything to this computer, so a turn's first reads, which are `request:` reads too, never lose the folder to a stale "at rest". A turn's end heard while an operation runs no longer counts the chat as in a turn, but lets go only after the agent says it is at rest. Landings, put-backs and thread copies are unchanged: only a chat's own host asks, and nothing is asked while no other chat waits.

**Wait instead of failing.** Today a second chat's host waits `LOCK_WAIT_MS` (10 s), fails, and every later tool call starts a new host and waits 10 s again. New, when the folder is held by another **chat's** host on this computer:

- the second chat's operation **waits** for the folder instead of failing, for up to **10 minutes**;
- while it waits, the chat shows the user that it is waiting for another chat working in the same folder. Use the existing `device.waiting` notice (`surogates/devices/waits.py`, which already has reasons `offline` and `copying`) with a new reason for this, shown in the chat like the others;
- when the folder frees, the operation runs and the chat continues by itself; the notice goes away;
- after 10 minutes it fails with a `busy` error whose message the agent can pass on: "Another chat on this computer has been working in this folder for more than 10 minutes, so this chat stopped waiting. Try again once it is done, or start a new chat in a new folder.";
- the server must not cut off an operation that is waiting for the folder before those 10 minutes (check the server-side timeouts for device operations).

When the folder is held by something other than a chat (a project landing, a put-back, an earlier holder `ToolHosts.holderOf` reports as such), keep today's behaviour and messages: those end on their own and already have their own waits. When the holder is not known to this app (another app instance, a crash leftover), keep today's short lock wait. (Builder's call, made in review.)

Fairness: when several chats wait for one folder, they get it in the order they asked.

### 5. Older desktop apps (web page and bridge)

The web page is served by the agent's server, so a new page can run in an older installed desktop app whose bridge only knows `"last"` and `"pick"` (`desktop/src/shell/bridge.ts` rejects anything else). The page must detect whether the desktop supports `"new"` (a bridge version or a feature check, per the bridge contract's rules in `web/src/lib/desktop-bridge-contract.d.ts`) and, if not, show today's line and link unchanged. Desktops already installed by colleagues are older than this change. (Builder's call, made in review.)

## Platforms

Linux and Windows both, tested live on each (Ubuntu laptop, Windows 11 VM). The folder lock differs per platform (an abstract socket on Linux, a named pipe on Windows, `desktop/src/hosts/folder-lock-*.ts`), and section 4 changes how long a host waits for it and when a host releases it, so turn-taking must be checked on both. macOS is not tested here.

## Testing

- Unit tests for `prepareFolder` with each choice, including `new` while a last folder exists, `last` with no usable last folder, and removal of a made folder not taken.
- Tests for the sheet content: busy and not busy, with and without a known title; button sets.
- Tests for turn-taking: a chat's turn end releases its host's folder at once; it does not while a background process runs; a second chat's operation waits and then runs when the folder frees; the waiting notice is sent and cleared; the 10-minute limit gives the new message; several waiters get the folder in order; landings, put-backs and unknown holders keep today's behaviour.
- Web: an older bridge without `"new"` shows today's line.
- Web tests for the menu: default `new`, selection changes the label and icon, the choice is passed to the bridge.
- Live, on Ubuntu and on Windows, as many of these as possible:
  - a new chat defaults to its own new folder; "the last folder" and "Choose a folder…" work; an untaken new folder is removed; a reopened old chat keeps its folder;
  - two chats in one folder: B asks while A works, sees the waiting notice, and continues by itself when A's answer ends; then A asks while B works, and waits in turn;
  - A runs a background command: B keeps waiting until it ends;
  - A's answer takes longer than 10 minutes: B stops waiting with the clear message;
  - three chats in one folder take turns in order;
  - the busy warning on the sheet, with "New folder" and "Browse…";
  - closing A's window or quitting the app while B waits; restarting the app mid-wait;
  - an older installed desktop with the new page shows today's line;
  - a project's threads and landings still work as before.

## Addendum

### 6. Turn-start reads wait their turn too

Server only: `surogates/session/files.py`, `surogates/devices/operations.py` and the harness callers.

**What went wrong.** Section 4 makes an agent's tool calls wait for a folder another chat holds: the desktop queues the operation (`ToolHosts.inTurn`, up to `FOLDER_WAIT_MS`) and says so with `op_waiting`, and the worker waits "however long that takes" (`DeviceOperations.run`). But at the start of a turn the harness reads the folder for itself, outside any tool call, under a fixed `asyncio.timeout(HARNESS_WITHIN_S)` of 10 s (`surogates/session/files.py:42`): the folder's AGENTS.md or CLAUDE.md for the system prompt (`_read_folder_context`, `surogates/harness/loop_context_replay.py:1024-1036`) and the folder's clock, the mark that says where the turn began (`_folder_cursor`, `surogates/harness/loop_artifact_completion.py:896-916`). These reads are device operations like any other (`session_files` builds a `JournalRunner` over `DeviceOperations.run`), so on the desktop they go through the same `ToolHosts.hostOf` and are queued behind the other chat, and the desktop does send `op_waiting` for them. The server never looks: `session_files` makes its `DeviceOperations` without a notice, so `_wait_watching` only awaits, and the 10 s deadline cuts the read off. The turn then runs without the folder's instructions file, lists none of the files it changed ("its computer did not say where this turn began"), and starts about 20 s late.

**New behaviour.** A read the harness makes in a chat's folder outside a tool call waits its turn like a tool call does:

- while the desktop says the operation waits for its folder (`waiting_key` holds `"folder"`), the read's deadline does not run out: it is kept four recheck intervals ahead (two rechecks and two polls' Redis patience), set before the chat is told, so a slow notice or a poll that times out does not end it;
- once the folder is free (the key is gone), the deadline is `HARNESS_WITHIN_S` from then, even near the limit below: the operation, and the reads after it in the same request, get the usual 10 s to be answered;
- the wait is shown in the chat with the same `device.waiting` reason `folder` notice as a tool call's, and cleared the same way;
- a folder wait never holds the deadline off past `HARNESS_WITHIN_S` plus `FOLDER_WAIT_S` (10 minutes, the desktop's own `FOLDER_WAIT_MS`) from the read's start.

It lives in one place: `session_files(..., within=HARNESS_WITHIN_S)` sets the deadline itself, and gives its `DeviceOperations` a notice and a `FolderPatience` over that deadline, which `_wait_watching` holds off or lets run as it polls the key. The four harness callers stop wrapping `session_files` in their own `asyncio.timeout` and pass `within` instead.

**Scope.** Every harness read or write of a local folder outside a tool call: the project context and the turn mark at the turn's start, and at its end the walk that lists the turn's files (`_walk_folder`) and a fenced SVG or HTML promoted to an artifact (`_promote_fenced_artifacts`). The turn-end two rarely wait, since the chat's own host still holds the folder until the desktop hears the turn ended, which comes after them; when they do (the chat went quiet past the host's idle time and another chat took the folder), waiting is still better than a turn that lists nothing or loses its artifact.

**Unchanged.**

- A folder no other chat holds: the 10 s deadline as today. A computer that is away is still said at once (`ComputerAway`), and one that is online but silent still gives up after 10 s, so a turn start is never stalled by a slow or absent computer.
- The file panel's reads and changes (`READ_WITHIN_S` 60 s, `CHANGE_WITHIN_S` 20 s, `surogates/api/routes/workspace.py:123-124`). They are HTTP requests: holding one open 10 minutes is no answer. A change already comes back as 202 and is joined again by the page; a read still says the computer did not answer in time. They make no `within` request, so they show no notice either: the API's notices would fight the worker's counts.
- Tool calls (`surogates/devices/sandbox.py`): they wait as before, with no deadline.
- The desktop. It already sends `op_waiting` for every operation it queues, whatever its kind (`desktop/test/tool-hosts.test.ts`, "tells when each operation starts and stops waiting for the folder").

**Edge cases.**

- The folder is freed mid-wait: the desktop clears the key, the next poll sees it, and the read has 10 s from then to finish. A read answered before the poll sees the key cleared also gets the fresh 10 s, so the reads after it in the same request are not cut short.
- The 10-minute limit is reached: the desktop answers the queued operation with its `busy` error ("Another chat on this computer has been working in this folder for more than 10 minutes, ..."), which the harness logs and goes on without, as for any failed read. Should the desktop never answer, the server's cap ends the read just after. The turn then runs without what it could not read, and goes on to its next folder request. The worst case: a turn's separate folder requests (the project context read, the turn mark, then its first tool call) can each wait up to the 10-minute limit while another chat keeps the folder that long.
- The computer goes away while the read waits: while the server sees the computer offline it does not hold the deadline off, and the link's end forgets the computer's folder waits (`forget_waits`), so the read gives up within about 10 s, as for a silent computer today. A link that drops and comes back within those seconds says the wait again after its welcome, and the read goes on waiting.
- Redis cannot tell whether the operation waits: the deadline is not held off, as today.
- The user stops the chat while a turn-start read waits: the Stop cancels the read at once, as it cancels a tool call (`AgentHarness._unless_stopped`), and the turn ends stopped, without waiting for the other chat. The cancel closes the read's operation, which tells the desktop to take it out of the folder's queue. The deadline running out cancels the read the same way. The turn-end walk and promotion run after the loop has ended, so a Stop does not reach them; they keep their own limit.
- A stopped turn hands the folder over like an ended one, once it has stopped: the harness ends it with a `session.pause` marked `turn_ended` (a channel stop with `session.stopped`, a failed turn with `session.fail`), and the desktop's turn follower (`followTurns`) tells the chat's host each of these as a turn end, not only `session.complete`, so the host lets the folder go at once rather than after `IDLE_MS`. The pause route's own `session.pause`, written before the turn hears of the Stop, is not a turn end: the stopped turn may still undo what it did in the folder. The window's notice that a chat's answer is ready (`followChat`) is still told only of a turn done.
- An old desktop sends no `op_waiting`: nothing holds the deadline off, and the reads give up after 10 s exactly as today.
- A worker that shuts down during the wait hands the turn to another worker, as for any wait on the computer; the read's operation is closed as it goes, since nothing would ask for it again, and the new worker reads again.
