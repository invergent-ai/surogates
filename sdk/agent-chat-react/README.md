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

## Read-only chats

A host that only watches a session passes `hideComposer` to `AgentChat`, and an adapter built with
`readOnlyAdapter` from its two reads, `getSession` and `openEventStream`:

```tsx
const adapter = readOnlyAdapter({ getSession, openEventStream });

<AgentChat adapter={adapter} sessionId={sessionId} hideComposer />;
```

`hideComposer` draws no composer: no input, nor its Files and Browser fold, its footer or the
integrations band, and the transcript fills the chat. It implies `disabled`, so nothing in the
transcript writes either: no Stop, no Retry, no answer to the agent's questions.

`readOnlyAdapter` takes from its source only the reads it names: `getSession` and
`openEventStream`, and optionally `pollEvents`, `getArtifact`, `getWorkspaceTree`,
`getWorkspaceFile`, `getBrowserState` and `getChatViewMode`. A write on a wider object never
passes through. Every write refuses with a `ReadOnlyChatError`, and so does a read the source
does not have; a source with no `getBrowserState` shows no browser. The adapter leaves out the
optional features, so the chat leaves them off.

## Documents

`DocumentEditor` opens a document in the document server's editor with no chat session behind it: its `open(theme)` answers `{document_server_url, config, mentionable_users?}`, the editor config the host signed, and it opens again when `reopenKey` or the theme changes, not for a new `open` alone. A failure to open shows its message in an alert, or "The document could not be opened." when it has none; `notices` shows the host's own lines under that alert, above the editor. `loadDocsApi` loads the document server's `api.js` once per server.
