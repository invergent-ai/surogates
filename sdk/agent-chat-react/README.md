# @invergent/agent-chat-react

Reusable React chat UI and runtime for Surogates agent sessions.

The package is adapter-driven. Consumers own routing, authentication, API base
URLs, workspace panels, and application shell state.

## Shared Panels

`ScheduledWorkPanel` renders user-owned scheduled work for an agent: fixed cron
jobs, dynamic `/loop` schedules, run counts, next/last run timestamps, last-run
links, and optional run-now/cancel actions. It is powered by optional adapter
methods:

- `listScheduledWork`
- `runScheduledWorkNow`
- `cancelScheduledWork`

If `listScheduledWork` is not implemented, the panel renders nothing.
