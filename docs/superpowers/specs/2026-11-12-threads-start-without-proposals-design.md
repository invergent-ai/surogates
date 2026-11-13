# Threads start without proposals

Addendum to `2026-11-12-agent-family-only-design.md`. Approved in chat on 2026-11-12.

## Why

The agent family moved a project's master onto `agent`. Proposals came with it as `agent(propose=true, where=…)`: one card per call, so the "Start all" button the old multi-thread card had disappeared. Claude Code has no proposal step: an agent starts its sub-agents, and asks in the conversation when it is unsure. Proposals existed for two reasons, and neither needs a card:

1. **Approval before work starts.** The master asks in chat ("I'd split this into three threads: A, B, C. Start them?") and starts them on yes.
2. **A project on the user's computer.** Only Surogate Desktop can make a thread's copy of the project's folder, and today it does so when the user presses Start on the card. The app already makes that copy with no dialog: the consent is the project's folder, chosen once. Only the trigger is the page's.

## What changes

- **The master starts every thread.** `agent` in a project's master starts a thread, in the cloud or on the user's computer. `propose` and `where` are gone from `agent`; `resolve` on `stop_agent` stays. A thread works where its project does: a project in the cloud has no threads on the user's computer, and its master points the user to a project made in Surogate Desktop for that.
- **A thread on the user's computer starts from the server.** The master's `agent` call makes the thread (not begun) and queues a `bind` for the project's computer that names the master instead of a page's confirmation token. Surogate Desktop accepts such a bind for a thread's copy when the master's chat is bound there to a project folder that is still the one its user chose, a copy can be made, and the folder the server names is that folder: the checks `prepareThreadFolder` makes today. It records the thread's binding to its own copy, with the project folder's mode. This grants nothing new: the master already works in the folder itself, and a thread's changes reach it only through the same landing.
- **The thread begins when its computer answers.** The server begins it (its row, its goal, the master's `worker.spawned`, its queue) as it records the computer's answer to that bind, once. A computer that is offline answers when it comes back; until then the thread has no row and the master was told it waits for the computer. A refusal (an older Surogate Desktop, the folder chosen again or gone) is told to the master as the thread's failure, with the computer's words.
- **No cards.** The `thread.proposed` event, the proposal card (and its "Start all"), the routes that start a thread from a card (`POST /v1/workstreams/{id}/threads`, `POST …/threads/{thread_id}/start`), a thread's `workstream_card`, the user-started `worker.spawned` news, and the desktop bridge's `prepareThreadFolder` and the web app's card flow are deleted. Old `thread.proposed` events in existing logs read as nothing.
- **The master's guidance** says to ask in chat before starting work the user did not ask for, and that a thread on the user's computer starts once that computer sets up its copy.

## What does not change

- `make_thread`, `begin_thread` and `start_thread` stay the thread machinery behind a master's `agent` (decided in chat): a thread is a chat of its own, with its own copy and landing, unlike a sub-agent. They lose the proposal and card parameters.
- A chat's own folder on the user's computer is still confirmed in the desktop's sheet, with its token.

## Release

The runtime ships first. An older Surogate Desktop answers the server's master-named bind with a refusal, and the thread fails to its master with "update Surogate Desktop", which is the right advice. The other order breaks the older web client the old runtime serves: the new app has no `prepareThreadFolder`, so a computer project's card says to update an app that is current, and a cloud project's card for the user's computer waits for a bind the new app no longer accepts.

## Not done

- A thread waiting for its computer has no row in the project until it begins. Showing it as waiting is left for later.
