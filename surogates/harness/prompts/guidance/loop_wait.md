---
name: Dynamic loop control
description: Guidance for dynamic /loop scheduled sessions.
---
# Dynamic Loop Control

This session was started by a dynamic `/loop` schedule. Before you finish,
call `loop_wait` with the next delay and a brief reason.

Pick a delay between 60 and 3600 seconds based on what you observed:
short waits for active work that is likely to change soon, longer waits when
there is nothing pending. Mention the chosen wait and reason in your final
response after the tool succeeds.
