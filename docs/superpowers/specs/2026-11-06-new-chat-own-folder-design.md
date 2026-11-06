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
