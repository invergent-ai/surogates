---
name: Cron loop iteration
description: Guidance for sessions running as one iteration of a cron `/loop` schedule.
---
# Cron Loop Iteration

This session was started by a cron schedule the user set up with `/loop`.
You are running **one iteration** of that schedule — the runner will wake
a fresh session on the configured cadence (e.g. every minute) and replay
the same prompt each time.

Do only this iteration's work and stop. Do not try to set up scheduling
for yourself: schedule-creation tools have been removed precisely because
nesting schedules from inside a scheduled run produces duplicate cron
jobs. Phrasings in the prompt like "do this every N minutes" or
"keep doing X until Y" describe the schedule the user **already**
configured — not work for you to schedule again.

If the prompt names a stop condition (e.g. "stop after 5 entries"), check
whether it has been met based on the workspace state you can observe.
When it has been met, call `loop_complete` with a brief reason — the
schedule's status flips to `completed` and no further runs are scheduled.
Without `loop_complete` (or a manual `/loop cancel` from the user) the
cron keeps waking on its cadence until expiry, even when there is nothing
left to do. Mention in your final response that the loop is complete.
