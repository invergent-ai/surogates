---
name: clarify
description: Tool-aware guidance for routing user-only missing context through the clarify tool and inbox.
applies_when: clarify tool is available
---
# Clarify tool usage
When progress requires information only the user can provide, call `clarify`.
Do not ask for missing user input in plain assistant text when `clarify` can
collect the answer.

Use `clarify` for:
- Missing account identifiers, usernames, preferences, or decisions that tools cannot retrieve.
- Browser handoff blockers such as login, MFA, CAPTCHA, cookie/account choices, or private content access.
- Any situation where the next step is waiting for the user to answer before useful work can continue.

Keep the question specific and actionable. Provide choices only when they
represent real options; otherwise ask a concise open-ended question. For
credential-sensitive flows, ask the user to take over the browser or provide a
non-secret identifier instead of asking them to reveal passwords or one-time
codes in chat.
