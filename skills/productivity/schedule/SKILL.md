---
name: schedule
description: "Create, change or cancel a scheduled prompt that runs automatically. Use when the user says things like \"every day\", \"each morning\", \"remind me in an hour\", \"run this at noon\", or wants to reschedule, edit or stop an existing scheduled task."
requires_tools:
  - cron_create
---

First, decide whether the user wants to **create a new** scheduled prompt or **change an existing** one.

## Changing an existing schedule

Call `cron_list` to find it — each entry has an `id`, `name`, `prompt`, `schedule` and `next_run_at`. There is no in-place edit: to reschedule it or change its prompt, `cron_delete` the old `id`, then create the replacement as below and tell the user you replaced it. To stop it, `cron_delete` it.

## Creating a new scheduled prompt

You are distilling the current session into a reusable shortcut. Follow these steps:

### 1. Analyze the session

Review the session history to identify the core task the user performed or requested. Distill it into a single, repeatable objective.

### 2. Draft the prompt

The prompt runs later as a fresh session — it must be entirely self-contained. That session will NOT have access to this one, so never reference "the current conversation," "the above," or any ephemeral context.

Include in the prompt:
- A clear objective statement (what to accomplish)
- Specific steps to execute
- Any relevant file paths, URLs, repositories, or tool names
- Expected output or success criteria
- Any constraints or preferences the user expressed

Write it in second-person imperative ("Check the inbox…", "Run the test suite…"). Keep it concise but complete enough that another session could execute it cold. A slash command (for example `/loop …`) is also a valid prompt.

### 3. Choose a name

Pick a short, descriptive name in kebab-case (e.g. "daily-inbox-summary", "weekly-dep-audit").

### 4. Determine the schedule

- `cron` is a 5-field expression (`minute hour day-of-month month day-of-week`): `0 8 * * 1-5` is 08:00 on weekdays, `*/30 * * * *` every half hour.
- A **one-time** run is `recurring: false` with an expression that matches the moment: "remind me at 15:00 today" is `0 15 * * *` with `recurring: false`. It fires once, at the next match.
- Pass `timezone` as an IANA name (`Europe/Bucharest`, `America/New_York`). It defaults to UTC, which is rarely what the user means by "9am" — use the timezone the user gave or that memory records, and ask if you don't know it.
- If the user didn't give a clear schedule, propose one and ask them to confirm before creating it.

Finally, call `cron_create` with `cron`, `prompt`, `name`, `timezone`, and `recurring`. Report the `next_run_at` it returns so the user can check the timing. If it refuses the prompt, tell the user why and rephrase only the part it names.
