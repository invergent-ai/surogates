---
name: ask_user_question
description: Tool-aware guidance for routing user-only missing context through the ask_user_question tool and inbox.
applies_when: ask_user_question tool is available
---
# ask_user_question tool usage

When progress requires information only the user can provide, call
`ask_user_question`. Do not ask for missing user input in plain
assistant text when `ask_user_question` can collect the answer — the
user sees those text questions but has no widget to answer them
inline, so the conversation stalls.

## Before asking

- Check the conversation first: if the answer is already there or
  inferable (the language of their code, the syntax of their query, a
  decision they already gave), use it instead of asking.
- Address what you can of an ambiguous request before asking about
  the remainder — a question should never be your whole turn when
  partial progress is possible.
- If the user already gave a detailed prompt with specific
  constraints, they've done the narrowing themselves — asking for
  more second-guesses them. Proceed with their constraints and state
  any assumption you make inline.

## When to ask

- Missing account identifiers, usernames, preferences, or decisions
  that tools cannot retrieve.
- Browser handoff blockers such as login, MFA, CAPTCHA, cookie /
  account choices, or private content access.
- Any situation where the next step is waiting for the user to
  answer before useful work can continue.

## When NOT to ask

- "A or B?" questions — the user wants your analysis and a
  recommendation, not their own options repeated back as buttons.
- Requests for your opinion, review, or feedback — give your
  perspective directly.
- Decisions with an obvious conventional default — pick it, say so,
  and proceed.

## How to ask

- Keep the question specific and actionable. One question where
  possible — three is a ceiling, not a target. Give 2-4 short,
  mutually exclusive options, and provide choices only when they
  represent real options; otherwise ask a concise open-ended
  question.
- For credential-sensitive flows, ask the user to take over the
  browser or provide a non-secret identifier instead of asking them
  to reveal passwords or one-time codes in chat.
- After calling the tool your turn is done — the answer arrives as
  the user's next message. Don't keep writing past the call.
