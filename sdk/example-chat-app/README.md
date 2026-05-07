# Agent Chat React Example App

Small runnable example for `@invergent/agent-chat-react`.

The app contains:

- A Vite React frontend that renders `AgentChat`.
- A browser adapter that implements the full `AgentChatAdapter` surface.
- A small Express backend that keeps sessions, events, artifacts, and workspace
  files in memory.
- A real OpenAI-compatible `/v1/chat/completions` streaming path for normal chat
  messages.
- Scripted slash commands for UI features that are not portable across every
  OpenAI-compatible provider.

## Setup

From the repository root:

```bash
pnpm install
cp sdk/example-chat-app/.env.example sdk/example-chat-app/.env
```

Edit `sdk/example-chat-app/.env`:

```bash
OPENAI_API_KEY=sk-your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
PORT=8787
```

Run the example:

```bash
pnpm -C sdk/example-chat-app dev
```

The Vite frontend starts on `http://localhost:5174` and proxies `/api` to the
local Express backend on `http://localhost:8787`.

## Real LLM Path

Normal chat messages are sent to:

```text
${OPENAI_BASE_URL}/chat/completions
```

with:

```json
{
  "model": "OPENAI_MODEL",
  "messages": [{ "role": "user", "content": "..." }],
  "stream": true,
  "stream_options": { "include_usage": true }
}
```

The backend maps streamed provider chunks into `agent-chat-react` SSE events:
`user.message`, `harness.wake`, `llm.request`, `llm.delta`, `llm.response`, and
`session.done`.

## Demo Slash Commands

Open the slash command menu in the composer or type one of these commands:

- `/demo-tools`: representative tool calls and results.
- `/demo-artifacts`: markdown, table, chart, HTML, and SVG artifacts.
- `/demo-clarify`: interactive clarify tool response flow.
- `/demo-expert`: `consult_expert` output with feedback controls.
- `/demo-errors`: policy denied and provider-style error states.
- `/demo-context`: clears the visible conversation with `context.compact`.

These commands are scripted UI demos. They do not depend on provider tool-calling
support.

## Workspace Demo

The workspace panel uses the example backend's in-memory files. Uploads and
deletes affect only the current Node process.

## Verification

```bash
pnpm -C sdk/example-chat-app typecheck
pnpm -C sdk/example-chat-app test
pnpm -C sdk/example-chat-app build
```

## Limits

This is an SDK example, not a production Surogates server. It intentionally omits
auth, tenancy, persistence, queues, sandboxing, governance enforcement, and the
production worker/tool router.
