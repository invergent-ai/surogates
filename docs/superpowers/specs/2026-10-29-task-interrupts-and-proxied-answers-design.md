# Task interrupts reach their worker, and a proxied chat can answer an open question

Date: 2026-10-29. Branch: `fix/task-interrupts-and-proxied-answers` (surogates, from `master` 544e2c1ae).
Companion: surogate-ops `fix/studio-typed-replies-answer-questions` sends the new flag.

## Why

1. **Task interrupts go to a channel nobody listens on.** `tasks/tools.py` publishes the task cancel,
   `worker_complete` and `worker_block` interrupts to `f"{INTERRUPT_CHANNEL_PREFIX}{session_id}"`
   (lines ~510, ~595, ~801), i.e. `surogates:interrupt<id>`. The dispatcher subscribes to
   `surogates:interrupt:*` (`orchestrator/dispatcher.py` ~988), and every other publisher (chat Stop, missions,
   channels, keel, scheduled) uses `...:{id}`. So cancelling a task doesn't interrupt its running worker, and a
   worker that reports done or blocked isn't stopped between iterations. The existing test
   `test_cancel_task_running_publishes_interrupt` asserts the wrong channel, so it pins the bug.
2. **A typed reply through a proxy never answers an open question.** `send_message` turns a typed message into
   the answer of a pending `ask_user_question` only when the path is not `/v1/api/...`
   (`api/routes/sessions.py` ~1291: "only real web users (an API service-account client cannot detect the
   reinterpretation)"). Studio's chats go through surogate-ops, which can only call surogates with a
   service-account token, and the auth middleware allows those only on `/v1/api/` (`tenant/auth/middleware.py`
   ~257). So in Studio a reply typed in the chat box is stored as a normal message, the question waits until its
   timeout (up to 30 minutes), and only choice clicks work.

## Goals

1. All three task interrupts publish to `f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}"`, like every other
   publisher. Fix the test that asserted the wrong channel, and add one that shows a published task interrupt
   reaches the dispatcher's subscription pattern.
2. `SendMessageRequest` gains an optional `answers_open_question: bool = False`. The typed-reply step runs when
   the path is not `/v1/api/` (as today) **or** the caller set the flag. Every other guard of that step is
   unchanged (only a message its person typed, an active session, pure text with no images or attachments, not a
   built-in command, the question still fresh). Callers that don't send the flag behave exactly as today, so API
   clients are unaffected unless they opt in. The field's description says what it does and that a caller sets
   it only for text a person typed in a chat that shows the question.

## Non-goals

- Changing the auth middleware or the paths service accounts may use.
- Any other interrupt publisher (they are already correct).

## Tests

- Interrupts: each of the three publishes `surogates:interrupt:<id>`; the corrected cancel test; a test that the
  channel matches the dispatcher's pattern.
- Typed reply on `/v1/api/sessions/{id}/messages` with a service-account token: with the flag, a pending question
  is answered (a response event, no normal message); without the flag, unchanged (normal message, question still
  pending); with the flag but an attachment, or a command, or no pending question, behaves as without it. The web
  path keeps working as today.
- Each guard's test fails without it.

## Constraints

Explicit staging; Conventional Commit titles; no AI attribution or private ids in commit or PR text; no em or en
dashes; never git stash.

## Live verification (Monica)

Local stack with this branch's API and worker, and ops on the companion branch: in the Studio copilot, answer a
question with choices by typing in the chat box (a choice's text, then free text): the copilot carries on at once.
