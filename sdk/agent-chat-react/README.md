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

## Documents

`DocumentEditor` opens a document in the document server's editor with no chat session behind it: its `open(theme)` answers `{document_server_url, config, mentionable_users?}`, the editor config the host signed, and it opens again when `reopenKey` or the theme changes, not for a new `open` alone. A failure to open shows its message in an alert, or "The document could not be opened." when it has none; `notices` shows the host's own lines under that alert, above the editor. `loadDocsApi` loads the document server's `api.js` once per server.
