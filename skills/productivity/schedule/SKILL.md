---
name: schedule
description: "Create, change, pause or cancel a prompt that runs on a schedule, in whatever language the user asks. Use when the user says things like \"every weekday at 8:30\", \"în fiecare zi lucrătoare la 8:30\", \"each morning\", \"remind me at noon\", or wants to see, reschedule, edit, pause or stop a scheduled task."
---

You make the schedule yourself with `cron_create`, once the user has approved it. Never ask the user to type a command or a cron expression. Never look for cron, crontab, systemd timers or any other scheduler in the sandbox or terminal: none is there, and nothing started there outlives this session.

Never say a schedule is set up, running or active unless a tool result in this conversation shows it. If you are not sure what exists, call `cron_list`.

If `cron_create` is not among your tools, this session cannot make schedules. If it is itself a scheduled run, its schedule already exists: do this run's work and nothing else. Otherwise tell the user plainly that schedules cannot be made from here.

## Seeing, changing, pausing and stopping

`cron_list` shows the agent's schedules you can see (active and paused by default; `status: "all"` for every one): `id`, `name`, `prompt`, `schedule`, `timezone`, `status`, `next_run_at`, `runs`, `last_run_at`, `last_error`. `cron_get` shows one in full, with why it ended.

- Change one with `cron_update`: a new `prompt`, `name`, or cadence given as in `cron_create`. Confirm it like a new one.
- `cron_pause` stops its runs until `cron_resume`; `cron_run_now` starts a run now; `cron_delete` cancels it for good.
- If it is unclear which schedule they mean, list them and ask.
- The user can also use `/loop list` and `/loop pause|resume|cancel <id>`.

## Creating a new schedule

The user may ask in any language. You turn the request into a cadence and an IANA timezone, and you speak to them about it in their language.

### 1. Write the prompt

It runs later as a fresh session that cannot see this conversation, so it must be entirely self-contained. Never refer to "the current conversation", "the above", or anything said only here. Write it in the user's language unless they ask otherwise.

Include:
- A clear objective (what to accomplish)
- The steps to take
- Any file paths, URLs, repositories, sources or tool names it needs
- The expected output (for example "exactly five bullets, each with a title and link")
- Constraints or preferences the user gave

Write it as an imperative, concise but complete enough to run cold. It cannot itself be a `/loop` command.

### 2. Choose the cadence

Give exactly one of:
- **`times`** (with optional **`days`**): local times of day, e.g. `times: ["08:30"], days: ["mon","tue","wed","thu","fri"]`. Best for "every weekday at 8:30" / "în fiecare zi lucrătoare la 8:30"; each time fires once a day, also on the days the clocks change.
- **`interval`**: a fixed period, e.g. `90m`, `2h`, `25h`, `3d`, for "every 90 minutes".
- **`at`**: one run, at a local date and time, e.g. `2026-10-28T15:00` for "remind me at 15:00 today".
- **`cron`**: a 5-field cron (`minute hour day-of-month month day-of-week`, day-of-week `0`–`6` from Sunday) for what the others cannot say, e.g. "the 1st of every month at 09:00" is `0 9 1 * *`.

**Timezone**: an IANA name (`Europe/Bucharest`, `America/New_York`) or `UTC`. Use the one the user gave or that memory records; if you don't know it, ask in the confirmation: "8:30" almost never means UTC.

### 3. Preview, confirm, create

1. Call `cron_create` with the cadence, `timezone`, `prompt` and a short kebab-case `name`. Until the user has approved it in this turn it makes nothing and returns `would`, with the schedule's `next_run_at` on its own clock (`2026-10-14T08:30:00+03:00`).
2. Ask with `ask_user_question`, in the user's language, with the approving choice FIRST: the cadence in plain words, the timezone, the next run, a one-line summary of the prompt, and that it runs until they cancel it (or once). For example:
   > Programez asta: de luni până vineri la 08:30, ora României (Europe/Bucharest). Prima rulare: miercuri, 14 octombrie, la 08:30. Ce face: un rezumat în cinci puncte al articolelor noi de pe arXiv despre agenți AI. Rulează până când îl anulezi.
   > Choices: "Da, programează" (first), "Schimb ceva".
3. If they pick the first choice, call `cron_create` again with the same arguments and tell them, in their language, that it is set and when it next runs. If they change something, adjust and ask again. If they don't answer or decline, make nothing.

Where there is no `ask_user_question` (an operator's own chat with it turned off), `cron_create` makes it at once: tell them what was made.

If it refuses the prompt, tell the user why and rephrase only the part it names.

## Limits to state plainly

- Each run's answer appears in this chat, or is posted to the channel the schedule was made in. It reaches email or anywhere else only through a tool this agent has.
- A schedule runs until it is cancelled (or once, for a one-off): for something that should stop on its own ("until the deploy is green"), say so in the prompt, and the run ends its schedule when that is true.
- A new run is skipped while the previous one is still going.
- If the user wants to set up a short loop themselves: `/loop 5m <prompt>`, `/loop every day at 9 in Europe/Bucharest <prompt>`.
