---
name: Dynamic loop control
description: Guidance for dynamic /loop scheduled sessions.
---
# Dynamic Loop Control

This session was started by a dynamic `/loop` schedule. Before you finish,
call `loop_wait` to decide what happens next.

If the loop should run again, pick a delay between 60 and 3600 seconds:
short waits for active work that is likely to change soon, longer waits when
there is nothing pending.

If the loop's task is finished and there is no future work to wait for —
the original goal was reached, the deadline passed, or further runs would
be no-ops — call `loop_wait` with `completed: true` and a brief reason.
The schedule will not run again. Without `completed: true` the loop keeps
waking on the chosen delay until you cancel it manually, even when there
is nothing left to do.

Mention the chosen outcome and reason in your final response after the
tool succeeds.
