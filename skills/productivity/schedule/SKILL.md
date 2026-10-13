---
name: schedule
description: "Create, change or cancel a prompt that runs on a schedule, in whatever language the user asks. Use when the user says things like \"every weekday at 8:30\", \"în fiecare zi lucrătoare la 8:30\", \"each morning\", \"remind me at noon\", or wants to reschedule, edit or stop a scheduled task."
---

You make the schedule yourself with `cron_create`, once the user has confirmed it. Never ask the user to type a command or a cron expression. Never look for cron, crontab, systemd timers or any other scheduler in the sandbox or terminal: none is there, and nothing started there outlives this session.

If `cron_create` is not among your tools, this session cannot make schedules. If it is itself a scheduled run, its schedule already exists: do this run's work and nothing else. Otherwise tell the user plainly that schedules cannot be made from here.

## Changing or stopping a schedule

Call `cron_list` to find it — each entry has an `id`, `name`, `prompt`, `schedule`, `timezone`, `next_run_at` and `expires_at`. There is no in-place edit: make the replacement as below (confirmed like any new one), then `cron_delete` the old `id`, and tell the user you replaced it. To stop one, `cron_delete` it; if it is unclear which one they mean, ask. The user can also see their schedules with `/loop list` and cancel one with `/loop cancel <id>`.

## Creating a new schedule

The user may ask in any language. You turn the request into a 5-field cron and an IANA timezone, and you speak to them about it in their language.

### 1. Write the prompt

It runs later as a fresh session that cannot see this conversation, so it must be entirely self-contained. Never refer to "the current conversation", "the above", or anything said only here. Write it in the user's language unless they ask otherwise.

Include:
- A clear objective (what to accomplish)
- The steps to take
- Any file paths, URLs, repositories, sources or tool names it needs
- The expected output (for example "exactly five bullets, each with a title and link")
- Constraints or preferences the user gave

Write it as an imperative, concise but complete enough to run cold. It cannot itself be a `/loop` command.

### 2. Translate the schedule

- **Cron**, 5 fields: `minute hour day-of-month month day-of-week`, day-of-week `0`–`6` from Sunday. "Every weekday at 8:30" / "în fiecare zi lucrătoare la 8:30" is `30 8 * * 1-5`; "Mondays and Thursdays at 17:00" is `0 17 * * 1,4`; "the 1st of every month at 09:00" is `0 9 1 * *`.
- **Timezone**: an IANA name (`Europe/Bucharest`, `America/New_York`) or `UTC`. Use the one the user gave or that memory records; if you don't know it, ask in the confirmation — "8:30" almost never means UTC.
- A one-time run ("remind me at 15:00 today") is `recurring: false` with a cron that matches the moment (`0 15 14 10 *`).

### 3. Preview, confirm, create

1. Call `cron_create` with `cron`, `timezone`, `prompt`, a short kebab-case `name` and `recurring`. Until the user has confirmed in this turn it makes nothing and returns `would_create`, with the schedule's `next_run_at` on its own clock (`2026-10-14T08:30:00+03:00`).
2. Ask with `ask_user_question`, in the user's language: the cadence in plain words, the timezone, the next run from `would_create`, a one-line summary of the prompt, and that it runs until they cancel it (or once). Offer to confirm or to change something. For example:
   > Programez asta: de luni până vineri la 08:30, ora României (Europe/Bucharest). Prima rulare: miercuri, 14 octombrie, la 08:30. Ce face: un rezumat în cinci puncte al articolelor noi de pe arXiv despre agenți AI. Rulează până când îl anulezi. Confirmi?
3. If they confirm, call `cron_create` again with the same arguments and tell them, in their language, that it is set and when it next runs. If they change something, adjust and ask again. If they don't answer or decline, make nothing.

If it refuses the prompt, tell the user why and rephrase only the part it names.

## Limits to state plainly

- Each run's answer appears in this chat, or is posted to the channel the schedule was made in. It reaches email or anywhere else only through a tool this agent has.
- A schedule runs until the user cancels it (or once, when not recurring), however often its cron fires: for something that should stop on its own ("until the deploy is green"), say so in the prompt, and the run ends its schedule when that is true.
- Cron cannot say "the first Monday of the month" or "every other week".
- If the user wants a short polling loop and to set it up themselves, `/loop 5m <prompt>` runs every 5 minutes and stops by itself after 3 days.
