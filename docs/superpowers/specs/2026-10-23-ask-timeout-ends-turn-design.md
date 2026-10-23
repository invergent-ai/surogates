# A timed-out question ends the turn

Date: 2026-10-23. Branch: `fix/ask-timeout-ends-turn`.

## Why

When `ask_user_question` gets no answer within 30 minutes
(`ASK_USER_QUESTION_MAX_WAIT_SECONDS`), the tool returns
`{"cancelled": true, "reason": "timeout"}` and the turn carries on with every tool available.
Models read that as a go-ahead. Twice in live tests the Studio copilot built after its
plan-approval question timed out: a skill on 2026-10-21 (28 s after the timeout), and on
2026-10-23 an agent 19 s after the timeout, even though its instructions (ops #580) now say
"No answer is not approval" and "never act on it". Its reasoning: "The user wants me to go ahead
and build it." In a second chat the copilot re-asked with a new `ask_user_question` right after
the timeout, which starts another 30-minute wait. A prompt rule does not hold; the runtime has to
stop the turn.

A question its user dismissed by typing a command already ends the turn
(`surogates/harness/loop.py`, `_question_dismissed` and the `question_dismissed` branch), and the
model reads at its next turn "The user typed a command instead of answering, so the question was
not answered. Ask it again if you still need the answer."

## Goals

1. **Timeout ends the turn, with one tools-off reply (Monica).** When an `ask_user_question` in
   this iteration comes back with `reason: "timeout"`:
   - the agent gets no further iteration with tools;
   - the harness makes one final LLM call with no tools (the existing `_request_final_summary`
     mechanism, with its own `reason`, e.g. `question_timed_out`), asking the agent to tell the
     person in one short line, in their language, that it will wait for their answer;
   - the turn then ends; the session sits idle with no timer.
   - If that final call fails, the turn ends with no message (no retry).
2. **The timeout result carries a detail sentence** (in `_DETAILS`, like `dismissed`): the
   question was not answered, and the model should ask it again if it still needs the answer.
   The model reads it at its next turn, when the person writes again.
3. **Calls after the question in the same batch don't run.** A batch holding
   `ask_user_question` runs sequentially (`NEVER_PARALLEL_TOOLS`, `surogates/harness/tool_exec.py`),
   so today a model that emits `[ask_user_question, create_skill]` gets `create_skill` run after
   the question times out. When an `ask_user_question` comes back cancelled for any reason, the
   remaining calls of that batch are skipped with the existing `make_skipped_tool_result`, so
   the history stays valid. *(My call, not Monica's: it applies to every cancel reason, since a
   call planned before an unanswered question is the same "no answer taken as yes".)*
4. **Same rule as dismissal for pending input:** if something the user said is already waiting
   for the model (`_said_to_the_model`), the turn goes on with it instead, exactly as the
   dismissed branch does.
5. **DOC-12, current-page note** (`surogates/harness/loop_messages.py`,
   `_page_view_context_note`): today it says `call read_doc("<docPath>")`, which loads the whole
   page. Name the page's doc path and tell the agent to search it or read only the section it
   needs (the copilot's `search_docs` and `read_doc(path, section)`), not the whole page.
6. **DOC-13, skills index** (`surogates/harness/prompt.py`, `_regular_skills_prompt`): the
   "Skills (mandatory) ... you MUST load it with `skill_view(name)`" text is rendered whenever an
   agent has skills, even when `skill_view` is not among its tools (the copilot; any agent with
   its Skills tools off). Reword it for that case so it never tells the agent to call a tool it
   lacks. Decided earlier: reword, don't hide the index.

## Non-goals

- Changing the 30-minute wait, or making it configurable.
- Changing what a typed command, a pause, or the channel-only reasons
  (`person_wrote_something_else`, `step_came_due`, `malformed_response`) do to the turn, beyond
  goal 3 (skipping the rest of their batch).
- The ops copilot instructions (shipped in ops #580; they stay as a second line of defence).
- The docs line in surogate-docs `work/tools/inbox.md` ("the agent gets a `cancelled` signal and
  decides what to do next"): a follow-up docs PR once this ships.

## Anti-goals

- Any path where the model gets a tool call after a timed-out question in the same turn.
- A loop: re-asking in the same turn, each wait parking a worker for 30 minutes.
- A message that claims something happened ("I've built it", "I'll go ahead") after a timeout.

## Constraints

- This changes every agent on the platform, not just the copilot: tests must cover a web
  (Studio) session and a messaging-channel session (the final call's wording already differs by
  `ADAPTER_CHANNELS`; the "in their language" line must hold on channels).
- Reuse `_request_final_summary` rather than a second no-tools path. Its request text is chosen
  by channel today; the timeout needs its own text for both kinds of session.
- Tests in surogates' existing harness test style; assert on the events the turn emits (no tool
  call after the timeout, one `llm.response` with the new finish reason, session completed),
  not on mocks receiving arguments.
- Repo rules: explicit staging, `type(scope): imperative summary`, no AI attribution.

## Settled details *(my calls, not Monica's)*

- The timeout branch sits next to the dismissed branch, after the case-step and form exits, and
  follows its `_said_to_the_model` rule. A budget or guard stop that fires in the same iteration
  keeps its own path (it already makes a tools-off final call).
- The caller completes the session (as the dismissed branch does with `_complete_session`);
  `_request_final_summary` gains a way to take the timeout's request text. Finish reason on the
  `llm.response` and the session-completion reason are both `question_timed_out`. No turn
  summary is written for it, like `budget_exhausted`.
- Request text has a web variant and a channel variant (the channel one keeps "in their language").
  The reply is an ordinary `llm.response`; channel delivery is unchanged.
- Adding `detail` to the timeout result is safe: the readers found (ops Studio
  `session-thread.tsx`, the agent-chat-react SDK) use only `cancelled` and `reason`, and the
  `dismissed` result already carries a `detail`.
- DOC-12: `docPath` is only sent by Studio for the copilot. The note still names no tool by a
  signature the agent may lack: it says to search the page or read only the section needed, and
  mentions `search_docs`/`read_doc` only if they are in the session's tools; otherwise it just
  names the doc path.
- DOC-13: the prompt builder already has `self._available_tools` (it gates `guidance/skills` on
  it); use it for the reworded index.

## Left to the builder

- Whether `_question_dismissed` is generalised (e.g. returns the reason) or a sibling check is
  added; keep one place that decides "this question went unanswered".
- Exact wording of the final-call request, the timeout detail sentence, and the DOC-12/13 text,
  within the goals above.

## Live verification (Monica)

Local stack with the worker and API running this branch. The 30-minute wait makes a real test
slow; for the live check only, the constant may be lowered in the local checkout and restored
(never committed). Check:
- a build chat whose plan approval times out: nothing is created, the copilot sends one short
  "I'll wait" line, and answering later continues the build;
- an interview question timing out: one short line, no second question widget;
- the copilot's system prompt no longer says `skill_view`, and the current-page note offers a
  search or a section.
