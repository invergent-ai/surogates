# Example Chat App Design

## Goal

Create `sdk/example-chat-app`, a small runnable SDK example that uses
`@invergent/agent-chat-react` against a real OpenAI-compatible
`/v1/chat/completions` streaming provider.

The app should demonstrate how a consumer wires the adapter-driven chat UI while
keeping LLM credentials server-side. It should exercise the major UI features in
`agent-chat-react` without requiring the production Surogates API, worker,
sandbox, database, queue, tenant auth, or tool router.

## Approach

Use a self-contained pnpm workspace package with two parts:

- A Vite React frontend that imports `@invergent/agent-chat-react` from the
  workspace and renders `AgentChat`.
- A small Node/Express backend that implements the `AgentChatAdapter` HTTP/SSE
  contract for the example app and calls an OpenAI-compatible chat-completions
  endpoint for normal chat messages.

This gives the example a real LLM path while still being easy to run locally.
The backend owns environment variables and never exposes the provider API key to
the browser.

## Runtime Configuration

The example backend reads:

- `OPENAI_API_KEY`: required provider API key.
- `OPENAI_BASE_URL`: optional base URL, defaulting to `https://api.openai.com/v1`.
- `OPENAI_MODEL`: optional model name, defaulting to a current mainstream chat
  model that supports chat-completions streaming.
- `PORT`: optional server port for local development.

The backend sends chat requests to `${OPENAI_BASE_URL}/chat/completions` with
`stream: true`, using the plain chat-completions message format. It must not use
the Responses API or provider-specific tool-calling behavior.

## Frontend

The frontend owns app shell state only:

- Current session id.
- Selected or created sessions.
- Basic connection/config status display.
- The adapter implementation that translates browser calls to the local example
  backend.

The main surface is the packaged `AgentChat` component. The example should avoid
duplicating chat UI features that already exist inside `agent-chat-react`.

The frontend adapter implements the full `AgentChatAdapter` surface:

- Session listing, creation, retrieval, retry, pause, and delete.
- Message send.
- Event stream opening via `EventSource`.
- Artifact retrieval.
- Clarify responses.
- Expert feedback.
- Slash command listing.
- Workspace tree, file read, upload, and delete.

## Backend Session Model

The backend stores example state in memory:

- Sessions keyed by session id.
- Append-only events per session, each with a monotonically increasing event id.
- Chat history for real LLM messages.
- Demo artifacts.
- Demo workspace files.
- Last user message for retry behavior.

In-memory storage is intentional. The package is an SDK example, not a
production server.

## SSE Event Contract

The backend exposes a session event stream endpoint that supports an `after`
cursor. It emits named SSE events matching `AgentChatEventType`, including:

- `session.start`
- `user.message`
- `harness.wake`
- `llm.request`
- `llm.delta`
- `llm.response`
- `tool.call`
- `tool.result`
- `artifact.created`
- `artifact.updated`
- `clarify.response`
- `expert.result`
- `expert.endorse`
- `expert.override`
- `policy.denied`
- `context.compact`
- `session.pause`
- `session.complete`
- `session.fail`
- `session.done`

Normal LLM messages emit `user.message`, `harness.wake`, `llm.request`, streamed
`llm.delta` chunks, a final `llm.response`, and `session.done`.

If the provider returns usage metadata in the final chat-completions stream
chunk, the backend maps it into `input_tokens`, `output_tokens`, `model`, and
related token fields on `llm.response`. If usage is unavailable, token fields are
zero and the model is still reported.

## Feature Demos

Because OpenAI-compatible providers vary in tool-calling support, non-chat UI
features are exercised with scripted slash commands instead of relying on model
tool calls.

The backend provides slash commands such as:

- `/demo-tools`: emits representative `tool.call` and `tool.result` events for
  common tool renderers.
- `/demo-artifacts`: creates markdown, table, chart, HTML, and SVG artifacts and
  emits `artifact.created` events.
- `/demo-clarify`: emits a running `clarify` tool call; submitted answers emit
  `clarify.response` and continue the demo.
- `/demo-expert`: emits a `consult_expert` tool call plus `expert.result`, then
  accepts expert feedback.
- `/demo-errors`: emits provider-style and policy-style error states.
- `/demo-context`: emits `context.compact` with the clear strategy.

These commands should be documented as UI demo commands, not production agent
features.

## Workspace Demo

The backend implements the workspace methods using in-memory files seeded with a
few small examples. Uploads add files to that in-memory workspace. Deletes remove
them. File reads return UTF-8 content for text files and may reject binary files
with a clear error.

This is enough to exercise the packaged workspace tree, file viewer, upload, and
delete behavior.

## Error Handling

Provider and transport failures are converted into `session.fail` events with
an `error_category`, `error_title`, `error_detail`, and `retryable` flag when
possible.

Pause/stop marks the session paused and emits terminal events. Retry replays the
last user message through the real LLM path when one exists.

## Testing

Add focused tests at the example package level:

- Adapter URL and EventSource wrapping behavior.
- Backend event store cursor replay.
- Chat-completions stream parsing for common chunk shapes.
- Scripted feature command event emission.
- Workspace upload/read/delete behavior.

These tests should use local fakes for HTTP and SSE. They should not require a
real provider API key.

## Documentation

The package includes:

- `README.md` with install and run commands.
- `.env.example` documenting `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
  `OPENAI_MODEL`, and `PORT`.
- Notes explaining which paths use a real LLM and which slash commands emit
  scripted UI demo events.

## Out Of Scope

- OpenAI Responses API.
- Provider-specific tool calling as a requirement.
- Production Surogates auth, tenancy, database, worker, sandbox, queue, object
  storage, or governance enforcement.
- A generic reusable backend package.
- Long-term persistence.
