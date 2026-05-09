# Session Title Generator Port Plan

**Goal:** Port Hermes-style automatic session title generation into Surogates so new sessions get a concise title after the first user-visible assistant response.

**Architecture:** Add a focused async title generator under `surogates/harness/`, persist titles through `SessionStore`, and trigger generation from the harness after the first completed assistant response. Keep generation best-effort and non-fatal.

## Tasks

- [x] **Step 1: Write failing tests**
  Cover title cleanup/truncation, auxiliary LLM failure handling, store persistence without overwriting existing titles, and harness trigger behavior after the first visible assistant response.

- [x] **Step 2: Add title generator module**
  Port the prompt and cleanup behavior from Hermes `title_generator.py` into an async Surogates module that uses the existing OpenAI-compatible client.

- [x] **Step 3: Add store persistence helper**
  Add `SessionStore.update_session_title_if_empty(session_id, title) -> bool` so generated titles never overwrite user-set or previously generated titles.

- [x] **Step 4: Wire harness trigger**
  Trigger best-effort generation after the first visible assistant response and before ending/completing the turn. Skip when a title already exists, when there is no user message, or when there is no assistant content.

- [x] **Step 5: Verify**
  Run focused title tests plus adjacent session/harness tests.
