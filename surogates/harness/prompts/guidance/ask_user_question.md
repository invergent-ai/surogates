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

Use `ask_user_question` for:
- Missing account identifiers, usernames, preferences, or decisions
  that tools cannot retrieve.
- Browser handoff blockers such as login, MFA, CAPTCHA, cookie /
  account choices, or private content access.
- Any situation where the next step is waiting for the user to
  answer before useful work can continue.

Keep the question specific and actionable. Provide choices only when
they represent real options; otherwise ask a concise open-ended
question. For credential-sensitive flows, ask the user to take over
the browser or provide a non-secret identifier instead of asking
them to reveal passwords or one-time codes in chat.
