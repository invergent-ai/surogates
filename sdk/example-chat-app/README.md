# Agent Chat React Example App

Small runnable example for `@invergent/agent-chat-react` and `@invergent/ui-sdk`.

The app contains:

- A Vite React frontend that renders `AgentChat` in a shell built from `ui-sdk`
  components (`Sidebar`, `ListRow`, `Button`, `EmptyState`), on the shared
  `ui-sdk` theme.
- A browser adapter that implements the `AgentChatAdapter` surface the chat
  uses.
- A small Express backend that keeps sessions, events, artifacts, and workspace
  files in memory. It needs no model, no API key and no network: it is a fake
  backend.

## Setup

From the repository root:

```bash
pnpm install
pnpm -C sdk/example-chat-app dev
```

The Vite frontend starts on `http://localhost:5174` and proxies `/api` to the
local Express backend on `http://localhost:8787` (`PORT` changes the backend's
port; the proxy in `vite.config.ts` must follow it).

## Fake Backend

A normal message gets a scripted reply, the same one for the same message. It
streams in small chunks as `llm.delta` events and ends with `llm.response` and
`session.done`, so the chat shows streaming, markdown, a table and a code block.
Stop ends a reply where it is, Retry runs it again, and the thumbs under a reply
are recorded as a `user.feedback` event. A session takes its title from its
first message.

Open the slash command menu in the composer or type one of these commands for
the parts that are not a plain reply:

- `/demo-tools`: representative tool calls and results.
- `/demo-artifacts`: markdown, table, chart, HTML, and SVG artifacts.
- `/demo-ask-user-question`: interactive ask_user_question tool response flow.
- `/demo-expert`: `consult_expert` output with feedback controls.
- `/demo-errors`: policy denied and provider-style error states.
- `/demo-context`, or the composer's own `/clear`: clears the visible conversation
  with `context.compact`.

Any other slash command (`/loop`, ...) is sent to the backend as a
message and answered with a one-line scripted reply; it does nothing.

## Workspace Demo

The workspace panel uses the example backend's in-memory files. Uploads and
deletes affect only the current Node process.

## Styles

`src/client/styles.css` imports what a host of the chat needs: Tailwind, the
`ui-sdk` theme, and the stylesheets of `streamdown` and `tw-shimmer`.
`shadcn-variants.css` is a copy of `shadcn/tailwind.css`, which `ui-sdk`'s
components rely on.

## Verification

```bash
pnpm -C sdk/example-chat-app typecheck
pnpm -C sdk/example-chat-app test
pnpm -C sdk/example-chat-app build
```

## Limits

This is an SDK example, not a production Surogates server. It intentionally omits
auth, tenancy, persistence, queues, sandboxing, governance enforcement, and the
production worker/tool router. The shell has no navigation below the `md`
breakpoint, where `ui-sdk`'s `Sidebar` is hidden; the chat itself works there.
The adapter leaves out the optional features that need a host page of their own
(missions, inbox, scheduled work, integrations), so the chat leaves them off.
