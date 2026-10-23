# A timed-out question ends the turn: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A question that times out ends the agent's turn with one tools-off "I'll wait" line, calls queued after an unanswered question never run, and two prompt texts stop naming tools the agent may not have.

**Architecture:** The harness already ends the turn when a question is dismissed by a typed command (`_question_dismissed` + the `question_dismissed` branch in `surogates/harness/loop.py`) and already has a tools-off final call (`_request_final_summary`, which also completes the session). The timeout joins the same check and reuses the final call with its own request text. The sequential tool runner (`surogates/harness/tool_exec.py`) skips the rest of a batch after an unanswered question using the existing `make_skipped_tool_result`.

**Tech Stack:** Python 3.12, pytest (+ testcontainers for `tests/integration`).

**Spec:** `docs/superpowers/specs/2026-10-23-ask-timeout-ends-turn-design.md`

## Global Constraints

- Worktree `/home/monica/invergent/.worktrees/surogates-G`, branch `fix/ask-timeout-ends-turn`. Work only there.
- Test command (from the worktree; integration tests need docker):
  `sg docker -c "/home/monica/invergent/surogates/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts='' <paths>"`
- Stage files explicitly by path; never `git add -A` / `git add .`.
- Commit messages `type(scope): imperative summary`, no `Co-Authored-By` or any AI attribution.
- The timeout reason string stays `"timeout"`; the new finish/completion reason is `question_timed_out`.
- Exact copy (the plan fixes it):
  - Timeout detail: `No answer came in time, so the question was not answered. Ask it again if you still need the answer.`
  - Final-call request, web: `Your question got no answer in time. Without calling any tools, tell the user in one short line that you'll wait for their answer. Don't say you did or will do anything else.`
  - Final-call request, messaging channel (`session.channel in ADAPTER_CHANNELS`): `Your question got no answer in time. Without calling any tools, tell the person you are talking with, in their language, in one short line, that you'll wait for their answer. Don't say you did or will do anything else.`

## Review Focus

- A timeout while the user already sent a message (`_said_to_the_model`) → the turn goes on with that message, no final call. Test in Task 1.
- A typed command over a question → unchanged: turn ends, no extra final call, existing dismissed tests still pass. Task 1 runs them.
- A batch `[ask_user_question, <other tool>]` whose question is answered → the other tool still runs. Test in Task 2.
- The final call's LLM failing → session still completes with `question_timed_out`, nothing retried. Covered by the existing `_request_final_summary` failure path; Task 1's unit test asserts the completion reason.
- An agent with skills but neither `skill_view` nor `skill_manage` → the index renders without naming `skill_view`. Test in Task 3.

---

### Task 1: A timed-out question ends the turn with one tools-off reply

**Files:**
- Modify: `surogates/tools/builtin/ask_user_question.py` (constants near `DISMISSED`, `_DETAILS`, the timeout `return` in `_wait_for_response`)
- Modify: `surogates/harness/loop.py` (`_question_dismissed` near line 6546, its call near 4150, the `question_dismissed` branch near 4300, `_request_final_summary`)
- Test: `tests/integration/test_command_wake_once.py` (next to the dismissed tests, ~line 2620), `tests/test_loop_turn_id.py` (next to `test_a_halted_channel_turn_still_replies_to_the_person`)

**Interfaces:**
- Produces:
  - `ask_user_question.TIMED_OUT = "timeout"` and `ask_user_question.TIMED_OUT_DETAIL` (the exact detail copy); `_DETAILS[TIMED_OUT] = TIMED_OUT_DETAIL`; the timeout `return` uses `TIMED_OUT`.
  - `loop._unanswered_question(tool_calls_raw: list[dict], tool_results: list[dict]) -> str | None`: replaces `_question_dismissed`; returns the `reason` of the first `ask_user_question` result in this batch whose JSON has `cancelled: true`, else `None`.
  - `AgentHarness._request_final_summary(..., reason: str = "budget_exhausted", final_request: str | None = None)`: when `final_request` is given it is appended instead of the channel/web summary text.
  - Module-level strings in `loop.py`: `QUESTION_TIMED_OUT_REQUEST` (web copy) and `QUESTION_TIMED_OUT_CHANNEL_REQUEST` (channel copy).

- [ ] **Step 1: Write the failing tests.**
  - Integration, `test_a_question_that_times_out_ends_its_turn_with_one_reply_and_no_tools(workers, monkeypatch)`: `monkeypatch.setattr(ask_user_question_module, "_MAX_WAIT_SECONDS", 0.5)`; `workers.replies.append(ASKS)` then `workers.replies.append(_final_response("I'll wait for your answer."))`; wake as `a_question_waits` does (without needing the inbox wait to stall) and await the wake. Assert:
    ```python
    got = await what_the_question_got(workers, chat)
    assert (got["cancelled"], got["reason"], got["detail"]) == (True, "timeout", TIMED_OUT_DETAIL)
    assert len(workers.requests) == 2
    assert workers.requests[-1][-1]["content"] == QUESTION_TIMED_OUT_REQUEST
    assert (await workers.said(chat))[-1] == "I'll wait for your answer."
    assert await workers.status(chat) == "completed"
    ```
    and that the last `LLM_RESPONSE` event's `finish_reason == "question_timed_out"`.
  - Integration, `test_a_question_that_times_out_while_its_user_said_more_goes_on_with_what_they_said`: same timeout, but emit a `USER_MESSAGE` `{"content": "Use the Q3 figures."}` before the wait ends (mirror `test_a_question_dismissed_while_its_user_said_more_...`). Assert `workers.requests[-1][-1] == {"role": "user", "content": "Use the Q3 figures."}` and no `LLM_RESPONSE` with `finish_reason == "question_timed_out"`.
  - Unit, in `tests/test_loop_turn_id.py`, parametrized `("web", QUESTION_TIMED_OUT_REQUEST)`, `("whatsapp", QUESTION_TIMED_OUT_CHANNEL_REQUEST)`: `test_a_timed_out_question_gets_one_tools_off_reply`, calling `_request_final_summary(..., reason="question_timed_out", final_request=<the harness's choice for that channel>)` the way the loop will (factor the choice as `loop._question_timed_out_request(session) -> str` and call that). Assert `"tools" not in sent[0]`, `sent[0]["messages"][-1]["content"] == expected`, finish reason and `_complete_session` reason both `"question_timed_out"`.

- [ ] **Step 2: Run them to verify they fail** (test command, the three test ids). Expected: ImportError / assertion failures.

- [ ] **Step 3: Implement.**
  - `ask_user_question.py`: the constants and `_DETAILS` entry above.
  - `loop.py`: `_unanswered_question` replaces `_question_dismissed`; the call site keeps one variable (e.g. `unanswered = _unanswered_question(...)`). In the branch near 4300, keep the dismissed behaviour for `reason == DISMISSED` exactly as now; add: if `reason == TIMED_OUT and not await self._said_to_the_model(session, steer_cursor)`, log, then `await self._request_final_summary(session, messages, system_prompt, lease, request=<as the budget path passes it>, cost_tracker=cost_tracker, turn_id=turn_id, iteration_index=<as the budget path computes it>, reason="question_timed_out", final_request=_question_timed_out_request(session))` and `return`. Other cancel reasons keep today's behaviour.
  - `_request_final_summary`: the `final_request` parameter; nothing else in it changes.

- [ ] **Step 4: Run** the three new tests plus `tests/integration/test_command_wake_once.py -k "dismiss or question"` and `tests/test_loop_turn_id.py`. Expected: all pass, the existing dismissed tests unchanged.

- [ ] **Step 5: Commit**
  ```bash
  git add surogates/tools/builtin/ask_user_question.py surogates/harness/loop.py tests/integration/test_command_wake_once.py tests/test_loop_turn_id.py
  git commit -m "fix(harness): end the turn when a question times out"
  ```

### Task 2: Calls after an unanswered question in the same batch don't run

**Files:**
- Modify: `surogates/harness/tool_exec.py` (the sequential runner, the loop that already skips on `interrupt_check()` around lines 1152-1240)
- Test: `tests/integration/test_command_wake_once.py`

**Interfaces:**
- Consumes: none from Task 1 beyond `"cancelled": true` in the question's JSON result.

- [ ] **Step 1: Write the failing tests.**
  - `test_calls_after_an_unanswered_question_in_its_batch_are_skipped`: a scripted reply whose `tool_calls` are `call_ask` (`ask_user_question`, as `ASKS`) then `call_after` with a builtin the test worker has registered (pick one with no outside effect, e.g. `todo`). Time the question out (`_MAX_WAIT_SECONDS = 0.5`). Assert the `TOOL_RESULT` for `call_after` has the content `make_skipped_tool_result({...call_after...})["content"]`.
  - `test_calls_after_an_answered_question_in_its_batch_still_run`: same batch, answer the question (as the existing answered-question tests do); assert `call_after`'s result is not the skipped content.

- [ ] **Step 2: Run them to verify they fail.** Expected: the first fails (the second call ran).

- [ ] **Step 3: Implement** in the sequential loop: after a call named `ask_user_question` returns, parse its content; if it is a dict with `cancelled` true, every remaining call in that batch gets `make_skipped_tool_result(tc)` (same as the interrupt path) and none is executed.

- [ ] **Step 4: Run** both tests, Task 1's tests, and `tests/integration/test_command_wake_once.py -k question`. Expected: pass.

- [ ] **Step 5: Commit**
  ```bash
  git add surogates/harness/tool_exec.py tests/integration/test_command_wake_once.py
  git commit -m "fix(harness): skip the calls queued after an unanswered question"
  ```

### Task 3: Prompt texts name only tools the agent has (DOC-12, DOC-13)

**Files:**
- Modify: `surogates/harness/loop_messages.py` (`_page_view_context_note`, the `docPath` sentence ~line 410)
- Modify: `surogates/harness/prompt.py` (`_regular_skills_prompt`, ~line 800)
- Test: `tests/test_loop_pending.py` or a new `tests/test_prompt_tool_mentions.py` (one file, both tests)

**Interfaces:** none.

- [ ] **Step 1: Write the failing tests.**
  - `test_page_note_points_at_a_section_not_the_whole_page`: `_page_view_context_note({"page": "Vault", "docPath": "work/settings/vault"})` contains `"work/settings/vault"`, contains `"section"`, and does not contain `"read_doc("`.
  - `test_skills_index_never_names_skill_view_without_it`: `PromptBuilder(tenant, skills=[<one skill>], available_tools={"read_file"})` built prompt contains the skill's name and not `"skill_view"`; with `available_tools={"skill_view"}` it still contains `"skill_view(name)"` (today's wording unchanged when the tool is there). Build the tenant/skill the way `tests/test_brainstorming_gate.py` does.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.**
  - Page note: replace the `read_doc` sentence with: `This page is documented at "{doc_path}": search that doc or read only the section you need, not the whole page.`
  - Skills index: when `"skill_view" not in self._available_tools`, render the heading and index with this lead instead of the mandatory sentence: `These skills are in this agent's library. You can't open them in this session, so treat their names and descriptions as context only.` and omit the closing "Only proceed without loading a skill" line. With `skill_view`, the text is unchanged.

- [ ] **Step 4: Run** the two tests plus `tests/test_brainstorming_gate.py tests/test_skill_view_handler.py tests/test_loop_pending.py`. Expected: pass.

- [ ] **Step 5: Commit**
  ```bash
  git add surogates/harness/loop_messages.py surogates/harness/prompt.py <the test file>
  git commit -m "fix(harness): name only tools the agent has in the page note and skills index"
  ```
