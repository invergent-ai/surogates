# @invergent/website-widget

AG-UI-compatible TypeScript client for the Surogates public-website channel. Wraps the channel-specific bootstrap, HttpOnly cookie, CSRF double-submit, and SSE stream behind a standard [AG-UI](https://docs.ag-ui.com/) `AbstractAgent` so any widget built against AG-UI works on top of Surogates with no custom glue.

## What you get

```ts
import { WebsiteAgent } from '@invergent/website-widget';

const agent = new WebsiteAgent({
  apiUrl: 'https://agent.acme.com',
  publishableKey: 'surg_wk_...',
});

agent.subscribe({
  onTextMessageContentEvent: ({ event }) => renderDelta(event.delta),
  onToolCallStartEvent:      ({ event }) => showToolPill(event.toolCallName),
  onRunFinishedEvent:        () => markDone(),
  onRunErrorEvent:           ({ event }) => showError(event.message),
});

agent.addMessage({ role: 'user', content: 'How do I cancel my subscription?' });
await agent.runAgent();
```

That's it. Everything else -- publishable-key verification, the HttpOnly + Secure + SameSite cookie, `X-CSRF-Token` on every POST, SSE reconnect with cursor, per-turn `RUN_STARTED`/`RUN_FINISHED`, mapping Surogates-native events (`llm.delta`, `tool.call`, `policy.denied`, `expert.delegation`) onto AG-UI's standard vocabulary -- happens inside `WebsiteAgent`.

## Why AG-UI

AG-UI is the industry-standard agent-to-UI protocol (CopilotKit, LangGraph, Mastra, CrewAI). A widget written against it today can swap the backend from Surogates to any other AG-UI-compatible agent without rewriting the frontend. You get:

- Typed streaming text (`TEXT_MESSAGE_START` / `CONTENT` / `END`)
- Typed tool calls with incremental argument streaming (`TOOL_CALL_*`)
- Reasoning visibility (`REASONING_*`)
- Run lifecycle (`RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR`)
- Step tracking (`STEP_STARTED` / `STEP_FINISHED`) for sub-agents and expert delegation
- Middleware, subscribers, and state management out of the box

Surogates-specific signals that don't have a first-class AG-UI equivalent (`memory.update`, `context.compact`, `session.reset`, `policy.denied`, internal saga steps) are forwarded as AG-UI `CUSTOM` events with the original Surogates event name in `name`. Consumers that want them can match on `name`; consumers that don't simply ignore them.

## Install

```bash
pnpm add @invergent/website-widget @ag-ui/client @ag-ui/core rxjs
```

`@ag-ui/client`, `@ag-ui/core`, and `rxjs` are **peer dependencies** -- they likely already exist in your app's bundle (especially if you're using CopilotKit or another AG-UI consumer), so we don't duplicate them.

### Drop-in widget (`<script>` tag)

For plain HTML sites without a bundler, load the IIFE build from a CDN and
call `mount()`. One call renders a complete, self-contained chat widget — a
floating launcher button that opens a streaming chat panel, fully isolated in
a Shadow DOM so it can't collide with your site's CSS. The bundle includes
AG-UI, RxJS, and the UI:

```html
<script src="https://cdn.jsdelivr.net/npm/@invergent/website-widget@2/dist/surogates-widget.global.js"></script>
<script>
  SurogatesWidget.mount({
    apiUrl: 'https://agent.acme.com',
    publishableKey: 'surg_wk_...',
    // Optional appearance:
    // title: 'Acme Support',
    // accentColor: '#4f46e5',
    // welcomeMessage: 'Hi! How can I help?',
    // position: 'bottom-right',
  });
</script>
```

`mount(config)` returns a `WidgetHandle` — `{ open(), close(), toggle(),
destroy(), agent }` — for programmatic control.

| Option | Type | Notes |
|---|---|---|
| `apiUrl` | string, required | Base URL of the Surogates API. |
| `publishableKey` | string, required | `surg_wk_...` key. Safe to embed in browser JS. |
| `title` | string | Header label. Defaults to the deployed agent's name. |
| `accentColor` | string | Brand colour (hex) for the launcher/header/bubbles. |
| `welcomeMessage` | string | Greeting shown before the first turn (Markdown). |
| `position` | `'bottom-right'` \| `'bottom-left'` | Launcher corner. Default `bottom-right`. |
| `target` | `HTMLElement` | Host element. Defaults to `document.body`. |
| `inline` | boolean | Render the panel inline (no floating launcher). |
| `openByDefault` | boolean | Open the panel on load. |

The IIFE also exposes the headless `WebsiteAgent`, `EventType`,
`AbstractAgent`, the error classes, and the `Translator` on
`window.SurogatesWidget` for hosts that want to build their own UI.

#### Bundler consumers

If you build your own React/Vue app, import the headless client from the
package root, or the self-mounting UI from the `./ui` subpath:

```ts
import { mount } from '@invergent/website-widget/ui';
mount({ apiUrl: 'https://agent.acme.com', publishableKey: 'surg_wk_...' });
```

## API

### `new WebsiteAgent(config)`

| Option | Type | Notes |
|---|---|---|
| `apiUrl` | string, required | Base URL of the Surogates API (e.g. `https://agent.acme.com`). No trailing slash required. |
| `publishableKey` | string, required | `surg_wk_...` key configured at deploy time via `website.publishable_key`. Safe to embed in browser JS. |
| `threadId` | string, optional | AG-UI thread id. One is minted if not provided. |
| `agentId` | string, optional | AG-UI agent id. |
| `initialMessages` | `Message[]`, optional | Pre-populated conversation history. |
| `initialState` | `State`, optional | Pre-populated agent state. |

Plus every other field accepted by AG-UI's `AgentConfig`.

### Inherited from `AbstractAgent`

* `runAgent(parameters?, subscriber?): Promise<RunAgentResult>` — primary entry point
* `subscribe(subscriber): { unsubscribe() }`
* `addMessage(message)` / `addMessages(messages)` / `setMessages(messages)`
* `abortRun()`
* `messages`, `state`, `threadId`, `agentId`

See [AG-UI docs](https://docs.ag-ui.com/sdk/js/client/abstract-agent) for the full interface.

### Additional methods

#### `ensureBootstrapped(): Promise<BootstrapResult>`
Exchange the publishable key for a session cookie + CSRF token. Called automatically by the first `runAgent()`; expose this to validate configuration eagerly (e.g. at widget-load time).

#### `end(): Promise<void>`
Mark the server-side session `completed` and clear the session cookie. Call when the visitor closes your chat UI.

### Error taxonomy

Every error the SDK throws or emits via `RUN_ERROR` derives from `SurogatesError`:

| Class | When |
|---|---|
| `SurogatesAuthError` | Publishable key invalid, Origin not in allow-list, CSRF mismatch. Non-retryable. |
| `SurogatesRateLimitError` | HTTP 429 or per-session message cap reached. Exposes `retryAfter` (seconds). |
| `SurogatesProtocolError` | Malformed response, SDK/server version mismatch. Priority-1 diagnostic signal. |
| `SurogatesNetworkError` | Network blip, DNS, CORS preflight refusal. Retryable. |

## Event mapping

The Surogates server emits event types defined in `surogates/session/events.py`. They map to AG-UI as follows:

| Surogates | AG-UI | Notes |
|---|---|---|
| `llm.delta` | `TEXT_MESSAGE_CHUNK` (role=`assistant`) | Expanded to `TEXT_MESSAGE_START/CONTENT/END` by AG-UI's client transform |
| `llm.response` | — (closes the running chunk stream) | Also drives end-of-turn detection |
| `llm.thinking` | `REASONING_START` + `REASONING_MESSAGE_START` + `REASONING_MESSAGE_CONTENT` | |
| `tool.call` | `TOOL_CALL_CHUNK` | Full args in one chunk; AG-UI expands to `TOOL_CALL_START/ARGS/END` |
| `tool.result` | `TOOL_CALL_RESULT` | |
| `expert.delegation` | `STEP_STARTED` (stepName=`expert:<name>`) | |
| `expert.result` | `STEP_FINISHED` | |
| `session.fail`, `harness.crash` | `RUN_ERROR` | Terminal for the run |
| `session.done`, `session.complete` | closes stream + emits `RUN_FINISHED` | |
| `policy.denied`, `memory.update`, `context.compact`, and every other Surogates-specific event | `CUSTOM` | `name` carries the original Surogates type |
| `user.message`, `llm.request`, `session.start`, `sandbox.*`, `policy.allowed`, `harness.wake` | dropped | Internal orchestration, not user-facing |

Plus the lifecycle envelope every run is wrapped in: `RUN_STARTED` at the top, `RUN_FINISHED` or `RUN_ERROR` at the bottom.

## Security model

The agent enforces the website channel's security contract transparently:

- **Publishable key** is sent only on bootstrap, only to the configured `apiUrl`, as `Authorization: Bearer`. Never persisted by the SDK.
- **Origin**: the browser sets it automatically on every cross-origin request; the server re-checks it on every call against the agent's allow-list.
- **Session cookie** is HttpOnly + Secure + SameSite=None, Path=/. Set by the server, managed by the browser.
- **CSRF**: the bootstrap response returns a CSRF token that the SDK caches in memory and attaches to every `POST` as `X-CSRF-Token`. The server compares it constant-time against the `csrf` claim baked into the cookie JWT.

See the [website channel documentation](https://github.com/invergent-ai/surogates/blob/master/docs/channels/website.md) for the full threat model and the server-side invariants.

## Development

```bash
pnpm install       # first time
pnpm test          # vitest
pnpm typecheck     # tsc --noEmit
pnpm build         # ESM + CJS + IIFE to ./dist
```

### Bundle size

Measured on the current build:

| Target | Raw | Gzipped |
|---|---|---|
| ESM headless (`dist/index.js` + chunk) | ~23 KB | **~6 KB** |
| ESM widget UI (`dist/ui.js`) | 16 KB | **~5 KB** |
| IIFE (`dist/surogates-widget.global.js`) | 178 KB | **~49 KB** |

The npm headless numbers exclude AG-UI, RxJS, and zod (peer deps); the headless
entry stays Preact-free. The `./ui` build bundles Preact (peers still external).
The IIFE bundles everything (AG-UI + RxJS + Preact + UI) for script-tag users and
is built with `platform: 'browser'` so transitive deps resolve to their
Web-Crypto variants rather than Node's `require('crypto')`.

## Versioning

This package follows semantic versioning; the wire protocol version is tracked separately in `PROTOCOL_VERSION`. The SDK sends `X-Surogates-Widget-Version: <semver>` on every request so server logs can correlate a buggy build with its error surface. A breaking change to the Surogates channel protocol bumps both `PROTOCOL_VERSION` and the major version of this package.

## License

AGPL-3.0-or-later (same as the parent Surogates project).
