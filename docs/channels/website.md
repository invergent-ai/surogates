# Website Channel

The website channel exposes an agent on a **public website** as a chat widget. Visitors are anonymous browser users with no platform account; identity is the server-side session cookie alone. Developers embed the agent using either the official [`@invergent/website-widget`](../../sdk/website-widget/README.md) JavaScript SDK ([AG-UI](https://docs.ag-ui.com/)-compatible) or plain `fetch` + `EventSource` against the raw HTTP surface documented below.

Authentication is a two-layer pattern borrowed from Stripe's publishable keys:

1. **Publishable key** (`surg_wk_…`) — safe to embed in browser JS. Authority is recognised only together with an `Origin` header listed in the agent's allow-list. A stolen key used from a different origin is rejected.
2. **Session cookie** — issued on bootstrap. HttpOnly + Secure + SameSite=None, scoped to `/`. Signed JWT with the session id, agent id, origin, and a CSRF token baked in.

State-changing endpoints require a double-submit CSRF token: the value of the `csrf` claim in the cookie JWT must match an `X-CSRF-Token` header on every POST.

## When to use it

| Use case | Example |
|---|---|
| Support widget | A bot on your docs site that answers product questions and files tickets |
| Sales assistant | A pricing-page chat that helps visitors choose a plan |
| Discovery tool | A help-me-find-the-right-feature conversation on a marketing site |

Do **not** use the website channel for authenticated end users; use the [web channel](web.md) so the session carries user identity and per-user memory.

Do **not** use the website channel for fire-and-forget backend pipelines; use the [API channel](api.md) with `surg_sk_` service-account tokens.

## Provisioning a website agent

Website agents are managed programmatically through `surogate-ops` using the `WebsiteAgentStore` Python API:

```python
from surogates.channels.website_agent_store import WebsiteAgentStore

store = WebsiteAgentStore(session_factory)
issued = await store.create(
    org_id=org_id,
    name="support-bot",
    allowed_origins=["https://customer.com", "https://www.customer.com"],
    tool_allow_list=["web_search", "clarify", "consult_expert"],
    system_prompt="You are the Acme product support agent...",
    model="gpt-5.4",
    session_message_cap=50,
    session_idle_minutes=30,
)
print(issued.publishable_key)   # surg_wk_… — surface this ONCE
```

The raw publishable key is returned exactly once. Only a SHA-256 digest is stored; if you lose it, rotate by deleting and recreating the agent.

### Key configuration fields

| Field | Purpose |
|---|---|
| `allowed_origins` | Exact-match list (scheme + host + port). Wildcards not supported. |
| `tool_allow_list` | Subset of tools the anonymous visitor may invoke. Empty = no restriction (falls back to platform governance). Ops should always set an explicit list. |
| `system_prompt` | Prepended to the harness system prompt. |
| `model` | Model override for this agent's sessions. |
| `skill_pins` | Skills pinned into every session the agent serves. |
| `session_message_cap` | 0 = unbounded. Enforced on each message submission. |
| `session_token_cap` | 0 = unbounded. Enforced by the harness on each LLM call. |
| `session_idle_minutes` | Idle timeout before the session is reset in place. |
| `enabled` | Disabling stops all in-flight sessions within the auth cache TTL (~30s). |

### Default tool allow-list rationale

Tools that are never appropriate for anonymous visitors:

* `terminal`, `execute_code`, `patch`, `write_file`, `read_file`, `search_files`, `list_files` — filesystem/shell access
* `skill_manage` — mutates tenant assets
* `delegate_task` — can spawn arbitrary sub-agents

Common safe defaults: `web_search`, `web_extract`, `clarify`, `consult_expert`, `todo`.

---

# Integration

Two supported paths. **Pick the SDK unless you have a specific reason not to** — it handles CSRF, cookie expiry recovery, cursor tracking, event translation, and the AG-UI lifecycle for you.

## Option 1: JavaScript SDK (recommended)

The [`@invergent/website-widget`](../../sdk/website-widget/README.md) package is a TypeScript SDK built around [AG-UI](https://docs.ag-ui.com/) — the industry-standard agent-to-UI protocol also used by CopilotKit, LangGraph, Mastra, and CrewAI. Widgets written against AG-UI work on top of Surogates with no custom glue.

### Install (npm / pnpm / yarn)

```bash
pnpm add @invergent/website-widget @ag-ui/client rxjs
```

`@ag-ui/client` and `rxjs` are peer dependencies — they likely exist in your app bundle already if you use CopilotKit or any other AG-UI consumer.

### CDN (plain `<script>` tag)

For sites without a bundler, use the IIFE build from a CDN. It bundles AG-UI + RxJS (~66 KB gzipped):

```html
<script src="https://cdn.surogates.com/widget/v1/surogates-widget.global.js"></script>
<script>
  const agent = new SurogatesWidget.WebsiteAgent({
    apiUrl: 'https://agent.acme.com',
    publishableKey: 'surg_wk_...',
  });
</script>
```

The IIFE global exposes `WebsiteAgent`, `EventType`, `AbstractAgent`, all four `Surogates*Error` classes, and the `Translator` class.

### Quick start

```ts
import { WebsiteAgent, EventType } from '@invergent/website-widget';

const agent = new WebsiteAgent({
  apiUrl: 'https://agent.acme.com',
  publishableKey: 'surg_wk_...',
});

agent.subscribe({
  onTextMessageContentEvent: ({ event }) => renderDelta(event.delta),
  onToolCallStartEvent:      ({ event }) => showToolPill(event.toolCallName),
  onRunFinishedEvent:        () => markTurnDone(),
  onRunErrorEvent:           ({ event }) => showError(event.message),
});

// User sent "How do I cancel my subscription?"
agent.addMessage({
  id: crypto.randomUUID(),
  role: 'user',
  content: 'How do I cancel my subscription?',
});
await agent.runAgent();
```

That's the happy path. Everything else — publishable-key verification, the HttpOnly + Secure + SameSite cookie, `X-CSRF-Token` on every POST, SSE cursor tracking across runs, per-turn `RUN_STARTED`/`RUN_FINISHED` bracketing, mapping Surogates-native wire events (`llm.delta`, `tool.call`, `expert.delegation`) onto AG-UI's standard vocabulary — happens inside `WebsiteAgent`.

## Option 2: Raw HTTP

Use the endpoint reference below if you're building a non-JS client, want maximum control, or don't want the AG-UI dependency. The SDK is the reference implementation of exactly this surface.

---

# SDK reference

## `new WebsiteAgent(config)`

Constructor config extends AG-UI's `AgentConfig`:

| Option | Type | Notes |
|---|---|---|
| `apiUrl` | `string` (required) | Base URL of the Surogates **API server**. No trailing slash required; stripped automatically. |
| `publishableKey` | `string` (required) | `surg_wk_...` key provisioned by ops via `WebsiteAgentStore.create()`. Safe to embed in browser JS. |
| `threadId` | `string` | AG-UI thread id. Minted if omitted. |
| `agentId` | `string` | AG-UI agent id. |
| `initialMessages` | `Message[]` | Pre-populated conversation history. |
| `initialState` | `State` | Pre-populated agent state. |
| `debug` | `boolean` or object | Enable the AG-UI debug logger. |

The agent does not talk to Surogates until the first `runAgent()` call; construction is cheap and synchronous.

## Methods inherited from `AbstractAgent`

See the [AG-UI AbstractAgent docs](https://docs.ag-ui.com/sdk/js/client/abstract-agent) for the full interface. The ones you'll use most:

| Method | Purpose |
|---|---|
| `runAgent(parameters?, subscriber?)` | Primary entry point. Bootstraps on first call, posts the latest user message, streams back events. Returns `Promise<RunAgentResult>` that resolves with `{ result, newMessages }` once `RUN_FINISHED` fires. |
| `subscribe(subscriber)` | Attach a long-lived event handler across multiple `runAgent` calls. Returns `{ unsubscribe() }`. |
| `addMessage(message)` / `addMessages(messages)` | Append to `agent.messages`. Typically called when the user sends input before `runAgent()`. |
| `setMessages(messages)` | Replace the conversation history wholesale. |
| `abortRun()` | Cancel the current run. |
| `messages`, `state`, `threadId`, `agentId` | Live public properties. |

## SDK-specific methods

### `ensureBootstrapped(): Promise<BootstrapResult>`

Exchange the publishable key for a session cookie + CSRF token. Called automatically by the first `runAgent()` — expose this if you want to validate configuration eagerly (e.g. surface a setup error at widget-load time before any user interaction):

```ts
try {
  await agent.ensureBootstrapped();
} catch (err) {
  if (err instanceof SurogatesAuthError) {
    showBanner('Chat is misconfigured. Contact support.');
  }
}
```

### `end(): Promise<void>`

Mark the server-side session `completed` and clear the cookie. Call when the visitor closes the chat UI so the session releases resources immediately instead of waiting for the idle-reset timer:

```ts
window.addEventListener('beforeunload', () => { agent.end(); });
```

Safe to call when no session has been bootstrapped yet.

## AG-UI events the SDK emits

The SDK emits standard [AG-UI events](https://docs.ag-ui.com/concepts/events). The most relevant for a widget:

| AG-UI event | Fired by | What to do |
|---|---|---|
| `RUN_STARTED` | Start of every `runAgent()` call | Show a typing indicator; lock the input. |
| `TEXT_MESSAGE_CHUNK` (or `TEXT_MESSAGE_START` / `CONTENT` / `END` after AG-UI's chunk transform) | `llm.delta` frames from the server | Append `event.delta` to the current assistant bubble. |
| `TOOL_CALL_CHUNK` (expanded to `TOOL_CALL_START` / `ARGS` / `END`) | `tool.call` frames | Render a "using tool …" pill or tool-trace row. |
| `TOOL_CALL_RESULT` | `tool.result` frames | Render the tool's output, if your UI shows it. |
| `REASONING_MESSAGE_CONTENT` | `llm.thinking` frames | Render chain-of-thought (if model + UI support it). |
| `STEP_STARTED` / `STEP_FINISHED` (with `stepName: "expert:<name>"`) | `expert.delegation` / `expert.result` | Render a "consulting `<expert>` …" progress row. |
| `CUSTOM` (with `name` set to the Surogates event type) | Everything else — `memory.update`, `context.compact`, `policy.denied`, saga events, ... | Optional; most UIs ignore. Match on `event.name` if you want to render specific Surogates-native signals. |
| `RUN_FINISHED` | End of turn | Unlock the input; finalise any in-progress animations. |
| `RUN_ERROR` | Non-recoverable failure | Show the error; `event.code` discriminates — `auth`, `sse_closed`, `error`, or the wire event type that caused a server-side failure (`session.fail`, `harness.crash`). |

Subscribe with `agent.subscribe({ onTextMessageContentEvent, onToolCallStartEvent, onRunFinishedEvent, onRunErrorEvent, ... })`. Each handler receives `{ event, messages, state, agent, input }` plus (for streaming handlers) useful accumulators like `textMessageBuffer` (full text so far) and `toolCallArgs` (parsed complete args on `TOOL_CALL_END`); see the [AG-UI AgentSubscriber docs](https://docs.ag-ui.com/sdk/js/client/subscriber) for the full handler surface.

## Complete example: minimal chat widget

A single-file TypeScript example that handles every major event category — streaming text chunks, tool-call lifecycle (start → args accumulation → end → result), reasoning, expert delegation, platform-specific `CUSTOM` events, errors, and cleanup. Drop this into your app and connect the `#chat` DOM stubs to whatever rendering you already have.

```ts
import {
  WebsiteAgent,
  SurogatesAuthError,
  SurogatesRateLimitError,
  SurogatesNetworkError,
  SURG_EVENT,
  type WebsiteAgentConfig,
} from '@invergent/website-widget';

// ---------------------------------------------------------------------------
// Widget state — what your UI needs to render.
// ---------------------------------------------------------------------------

interface ToolCallView {
  id: string;
  name: string;
  args: string;           // partial JSON as it streams in
  result?: string;        // populated on TOOL_CALL_RESULT
}

interface MessageView {
  id: string;
  role: 'user' | 'assistant' | 'reasoning';
  text: string;
  toolCalls: ToolCallView[];
}

const state = {
  messages: [] as MessageView[],
  currentAssistant: null as MessageView | null,
  currentReasoning: null as MessageView | null,
  running: false,
};

// ---------------------------------------------------------------------------
// Agent setup.
// ---------------------------------------------------------------------------

const agent = new WebsiteAgent({
  apiUrl: 'https://agent.acme.com',
  publishableKey: 'surg_wk_...',
} satisfies WebsiteAgentConfig);

// Validate config eagerly so misconfiguration surfaces before the
// visitor types anything.  If this throws SurogatesAuthError the key
// is wrong or the origin isn't in the allow-list.
agent.ensureBootstrapped().catch((err) => {
  if (err instanceof SurogatesAuthError) {
    showFatal(`Chat unavailable: ${err.detail ?? err.message}`);
  }
});

// ---------------------------------------------------------------------------
// Subscribe to the full AG-UI event surface.
// ---------------------------------------------------------------------------

agent.subscribe({
  // --- Run lifecycle -------------------------------------------------------

  onRunStartedEvent: () => {
    state.running = true;
    renderStatus('agent is thinking…');
  },

  onRunFinishedEvent: () => {
    state.running = false;
    state.currentAssistant = null;
    state.currentReasoning = null;
    renderStatus('');
    enableInput();
  },

  onRunErrorEvent: ({ event }) => {
    state.running = false;
    enableInput();
    // event.code discriminates the failure class so the UX can differ:
    // 'auth' → publishable key / origin / CSRF wrong -- config issue
    // 'sse_closed' → transport dropped, retry is safe
    // 'session.fail' / 'harness.crash' → server-side terminal
    // 'error' → uncategorised; surface the message and offer retry
    switch (event.code) {
      case 'auth':
        showFatal('Chat session expired. Please reload the page.');
        break;
      case 'sse_closed':
        showTransient('Connection dropped. Retrying…');
        retryWithBackoff();
        break;
      default:
        showTransient(event.message);
    }
  },

  // --- Streaming assistant text --------------------------------------------

  onTextMessageStartEvent: ({ event }) => {
    // A new assistant message turn begins.  Open a bubble that
    // onTextMessageContentEvent will progressively fill.
    state.currentAssistant = {
      id: event.messageId,
      role: 'assistant',
      text: '',
      toolCalls: [],
    };
    state.messages.push(state.currentAssistant);
    renderMessages();
  },

  onTextMessageContentEvent: ({ event, textMessageBuffer }) => {
    // Prefer ``textMessageBuffer`` (the accumulated string so far) over
    // manually concatenating ``event.delta`` -- AG-UI gives you the
    // running total for free, and it matches ``agent.messages`` after
    // RUN_FINISHED finalises the message.
    if (state.currentAssistant?.id === event.messageId) {
      state.currentAssistant.text = textMessageBuffer;
      renderMessages();
    }
  },

  onTextMessageEndEvent: ({ event, textMessageBuffer }) => {
    // Finalise the bubble.  At this point ``agent.messages`` already
    // contains the canonical assistant message with role='assistant'
    // and the full ``content``.
    if (state.currentAssistant?.id === event.messageId) {
      state.currentAssistant.text = textMessageBuffer;
      state.currentAssistant = null;
      renderMessages();
    }
  },

  // --- Tool call lifecycle -------------------------------------------------

  onToolCallStartEvent: ({ event }) => {
    // The assistant is about to invoke a tool.  Render a "using X…"
    // row inside the current assistant bubble.  ``parentMessageId``,
    // when present, tells you which assistant message this call
    // belongs to -- useful if a turn involves multiple parallel tool
    // calls.
    const call: ToolCallView = {
      id: event.toolCallId,
      name: event.toolCallName,
      args: '',
    };
    const parent =
      state.messages.find((m) => m.id === event.parentMessageId) ??
      state.currentAssistant;
    parent?.toolCalls.push(call);
    renderMessages();
  },

  onToolCallArgsEvent: ({ event, toolCallBuffer }) => {
    // Args stream in as JSON fragments; ``toolCallBuffer`` is the
    // accumulated JSON so far.  Surogates emits the full args in one
    // TOOL_CALL_ARGS event, but other backends may chunk -- this code
    // works for both.  ``partialToolCallArgs`` is the buffer parsed
    // best-effort if you want a typed view while it's still partial.
    for (const msg of state.messages) {
      const call = msg.toolCalls.find((c) => c.id === event.toolCallId);
      if (call) {
        call.args = toolCallBuffer;
        renderMessages();
        break;
      }
    }
  },

  onToolCallEndEvent: ({ event, toolCallName, toolCallArgs }) => {
    // Fully-parsed args are handed to you as ``toolCallArgs``; no need
    // to JSON.parse the buffer yourself.  Useful for inspecting what
    // the LLM decided to call the tool with before the result arrives.
    console.debug(`Tool ${toolCallName}(${JSON.stringify(toolCallArgs)}) running…`);
    // Your UI might swap the "using X…" row to a spinner here.
    void event;  // available; no extra action needed in this example
  },

  onToolCallResultEvent: ({ event }) => {
    // The tool finished and returned ``event.content`` (string, usually
    // JSON).  Pair with the call via ``toolCallId``.
    for (const msg of state.messages) {
      const call = msg.toolCalls.find((c) => c.id === event.toolCallId);
      if (call) {
        call.result = event.content;
        renderMessages();
        break;
      }
    }
  },

  // --- Reasoning (chain-of-thought) ---------------------------------------

  onReasoningMessageStartEvent: ({ event }) => {
    state.currentReasoning = {
      id: event.messageId,
      role: 'reasoning',
      text: '',
      toolCalls: [],
    };
    state.messages.push(state.currentReasoning);
    renderMessages();
  },

  onReasoningMessageContentEvent: ({ event }) => {
    // Reasoning content is opt-in in your UI; some widgets show it
    // collapsed behind a "Show thinking" toggle.
    if (state.currentReasoning?.id === event.messageId) {
      state.currentReasoning.text += event.delta;
      renderMessages();
    }
  },

  onReasoningMessageEndEvent: () => {
    state.currentReasoning = null;
  },

  // --- Expert delegation (STEP events) ------------------------------------

  onStepStartedEvent: ({ event }) => {
    // Surogates emits STEP events for expert-delegation (see
    // ``expert.delegation`` / ``expert.result`` in the wire protocol).
    // The translator formats ``stepName`` as ``expert:<name>``.
    if (event.stepName.startsWith('expert:')) {
      const expertName = event.stepName.slice('expert:'.length);
      renderStatus(`consulting ${expertName}…`);
    }
  },

  onStepFinishedEvent: ({ event }) => {
    if (event.stepName.startsWith('expert:')) {
      renderStatus('');
    }
  },

  // --- Platform-specific CUSTOM events ------------------------------------

  onCustomEvent: ({ event }) => {
    // Everything Surogates-native without a first-class AG-UI mapping
    // arrives here.  ``event.name`` is the original Surogates event
    // type; ``event.value`` carries the payload.  Most widgets ignore
    // all of these; match on ``name`` for the ones you want.
    switch (event.name) {
      case SURG_EVENT.POLICY_DENIED:
        // The harness refused a tool the LLM tried to call.  The
        // LLM's next message will explain; you can optionally show a
        // "blocked" badge in the meantime.
        console.info('tool blocked:', event.value);
        break;
      case SURG_EVENT.MEMORY_UPDATE:
      case SURG_EVENT.CONTEXT_COMPACT:
        // Informational.  Not user-facing in most widgets.
        break;
      default:
        // Unknown custom event -- forward-compatible no-op.
        break;
    }
  },

  // --- State / messages snapshots (optional) -------------------------------

  onMessagesChanged: ({ messages }) => {
    // Fires whenever ``agent.messages`` changes -- after every
    // TEXT_MESSAGE_END, tool result, etc.  A React app would use this
    // to re-render off a single source of truth instead of threading
    // deltas through the handlers above.
    console.debug(`agent.messages now has ${messages.length} entries`);
  },
});

// ---------------------------------------------------------------------------
// Sending a user message.
// ---------------------------------------------------------------------------

async function sendMessage(text: string): Promise<void> {
  if (state.running) return;               // one turn at a time
  if (!text.trim()) return;

  // Push the user message to local state for an instant echo, then
  // also tell the agent so it becomes part of ``agent.messages`` and
  // gets sent on the next POST.
  const userMsg: MessageView = {
    id: `u-${Date.now()}`,
    role: 'user',
    text,
    toolCalls: [],
  };
  state.messages.push(userMsg);
  renderMessages();

  agent.addMessage({ id: userMsg.id, role: 'user', content: text });

  disableInput();
  try {
    await agent.runAgent();
  } catch (err) {
    // Most errors come through onRunErrorEvent; this catch handles
    // hard bugs (rejected Promise inside the observable factory).
    if (err instanceof SurogatesRateLimitError) {
      showTransient(`Slow down -- try again in ${err.retryAfter ?? 30}s.`);
    } else if (err instanceof SurogatesNetworkError) {
      showTransient('Network error. Please retry.');
    } else {
      console.error(err);
    }
    enableInput();
  }
}

// ---------------------------------------------------------------------------
// Cleanup.
// ---------------------------------------------------------------------------

window.addEventListener('beforeunload', () => {
  // Gracefully mark the server-side session completed so the idle-
  // reset job doesn't have to time it out.  Fire-and-forget; the SDK
  // swallows errors from end() by design.
  void agent.end();
});

// ---------------------------------------------------------------------------
// Rendering stubs -- replace with your framework of choice.
// ---------------------------------------------------------------------------

function renderMessages(): void { /* iterate state.messages, paint */ }
function renderStatus(_status: string): void { /* show/hide banner */ }
function showFatal(_msg: string): void { /* non-recoverable UI */ }
function showTransient(_msg: string): void { /* toast / snackbar */ }
function enableInput(): void { /* ... */ }
function disableInput(): void { /* ... */ }
function retryWithBackoff(): void { /* re-open the agent or reload */ }

// Wire up your input element somewhere:
//   inputElement.addEventListener('submit', () => sendMessage(input.value));
```

### Same pattern in React

The subscriber handlers above don't know about React; lift them into a custom hook that bridges the agent's events to your component state:

```tsx
import { useEffect, useMemo, useState } from 'react';
import { WebsiteAgent, SurogatesAuthError } from '@invergent/website-widget';
import type { Message } from '@ag-ui/client';

export function useWebsiteAgent(apiUrl: string, publishableKey: string) {
  const agent = useMemo(
    () => new WebsiteAgent({ apiUrl, publishableKey }),
    [apiUrl, publishableKey],
  );

  const [messages, setMessages] = useState<Message[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // ``onMessagesChanged`` fires on every internal update so React
    // re-renders once per meaningful change instead of once per delta.
    const sub = agent.subscribe({
      onRunStartedEvent: () => { setRunning(true); setError(null); },
      onRunFinishedEvent: () => setRunning(false),
      onRunErrorEvent: ({ event }) => {
        setRunning(false);
        setError(event.message);
      },
      onMessagesChanged: ({ messages }) => setMessages([...messages]),
    });

    // Eager auth check; reflect misconfiguration immediately.
    agent.ensureBootstrapped().catch((err) => {
      if (err instanceof SurogatesAuthError) setError(err.message);
    });

    return () => {
      sub.unsubscribe();
      void agent.end();
    };
  }, [agent]);

  const send = async (text: string): Promise<void> => {
    agent.addMessage({ id: crypto.randomUUID(), role: 'user', content: text });
    await agent.runAgent();
  };

  return { messages, running, error, send };
}

// Usage:
//   const { messages, running, error, send } = useWebsiteAgent(apiUrl, key);
```

The `onMessagesChanged` callback is the React-friendly path: AG-UI owns the canonical conversation state on `agent.messages`, and the hook just mirrors it into React. Streaming deltas, tool calls, and reasoning all fold into `agent.messages` before `onMessagesChanged` fires, so a single state update per event keeps re-renders proportional to what the user actually sees.

## Errors

Every error the SDK throws or emits via `RUN_ERROR` derives from `SurogatesError`:

| Class | When | Retryable |
|---|---|---|
| `SurogatesAuthError` | Publishable key invalid, Origin not in allow-list, CSRF mismatch, cookie expired. | No (fix the config). The SDK auto-retries *once* on a cookie-expiry 401 via transparent re-bootstrap before surfacing. |
| `SurogatesRateLimitError` | HTTP 429 or per-session message cap reached. Carries `retryAfter` (seconds) when the server sends `Retry-After`. | Yes, after `retryAfter`. |
| `SurogatesNetworkError` | Network blip, DNS, CORS preflight refusal, **5xx from upstream** (ingress hiccup, worker restart). | Yes. |
| `SurogatesProtocolError` | Malformed response from the server, SDK/server version mismatch. | No — escalate as a priority-1 diagnostic. |

All carry `status` and `detail` when they originated from an HTTP response. Catch `SurogatesError` for a single catch-all; narrow with `instanceof` on the subclasses for specific UX affordances.

---

# HTTP API reference

All website-channel endpoints live under `/v1/website/*`. They are exempt from the platform's global JWT middleware and run their own authentication. The SDK targets exactly these routes.

### POST /v1/website/sessions — bootstrap

Exchanges a publishable key + allowed origin for a session cookie.

```
POST /v1/website/sessions
Authorization: Bearer surg_wk_...
Origin: https://customer.com
```

Response (`201 Created`):

```json
{
  "session_id": "8f...",
  "csrf_token": "hL7q...",
  "expires_at": 1714567890,
  "agent_name": "support-bot"
}
```

`Set-Cookie` header sets `surg_ws=…; HttpOnly; Secure; SameSite=None; Path=/; Max-Age=3600`. The browser holds the CSRF token in memory and echoes it on every POST. `Path=/` is intentionally broad: the `StripApiPrefixMiddleware` rewrites `/api/v1/...` to `/v1/...` on the server, so pinning the cookie to either form alone would break the other. HttpOnly + the `website_session` JWT `type` claim prevent cross-route leakage.

### POST /v1/website/sessions/{id}/messages — send a message

```
POST /v1/website/sessions/8f.../messages
Origin: https://customer.com
X-CSRF-Token: hL7q...
Cookie: surg_ws=...
Content-Type: application/json

{"content": "How do I cancel my subscription?"}
```

Response (`202 Accepted`):

```json
{"event_id": 42, "status": "processing"}
```

### GET /v1/website/sessions/{id}/events — stream events (SSE)

```
GET /v1/website/sessions/8f.../events?after=0
Origin: https://customer.com
Accept: text/event-stream
Cookie: surg_ws=...
```

`EventSource` cannot set custom headers, so no `X-CSRF-Token` is required (SSE is a GET; CSRF protection targets state-changing requests). Authentication is cookie + origin.

Stream carries Surogates-native event names: `event: llm.delta`, `event: llm.response`, `event: tool.call`, `event: tool.result`, `event: policy.denied`, `event: expert.delegation`, `event: expert.result`, `event: session.done`, etc. The SDK's `Translator` maps these to AG-UI events; raw-HTTP consumers handle them directly.

A `session.done` event with `retry: 0` is sent when the session enters a terminal state; the client should stop reconnecting.

### POST /v1/website/sessions/{id}/end — end the session

Optional. Marks the session completed and clears the cookie. Requires cookie + CSRF.

```
POST /v1/website/sessions/8f.../end
Origin: https://customer.com
X-CSRF-Token: hL7q...
Cookie: surg_ws=...
```

Response (`204 No Content`) with a `Set-Cookie` that deletes `surg_ws`.

## Raw HTTP integration sketch

For clients that cannot or will not use the SDK:

```js
const PUBLISHABLE_KEY = "surg_wk_...";
const API = "https://agent.acme.com";

// 1. Bootstrap
const boot = await fetch(`${API}/v1/website/sessions`, {
  method: "POST",
  credentials: "include",            // accept the cookie
  headers: {
    "Authorization": `Bearer ${PUBLISHABLE_KEY}`,
    "Content-Type": "application/json",
  },
});
const { session_id, csrf_token } = await boot.json();

// 2. Subscribe to events (cookie sent automatically)
const stream = new EventSource(
  `${API}/v1/website/sessions/${session_id}/events`,
  { withCredentials: true },
);
stream.addEventListener("llm.response", (e) => render(JSON.parse(e.data)));
stream.addEventListener("session.done", () => stream.close());

// 3. Send a message
await fetch(`${API}/v1/website/sessions/${session_id}/messages`, {
  method: "POST",
  credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf_token,
  },
  body: JSON.stringify({ content: userInput }),
});
```

Note: this sketch omits reconnect-with-cursor (`?after=N`), cookie-expiry recovery (401 → re-bootstrap), and end-of-turn detection. The SDK handles all three.

### CORS requirements

For `credentials: "include"` to work, the browser requires:

* `Access-Control-Allow-Origin` set to your exact origin (not `*`)
* `Access-Control-Allow-Credentials: true`
* Preflight must return 204 with `Access-Control-Allow-Headers` listing `Content-Type, Authorization, X-CSRF-Token`

The platform's `/v1/website/*` CORS middleware handles all of this per-agent. Preflight (`OPTIONS`) is answered permissively because the browser strips auth from preflights — the actual authorisation happens on the follow-up request, which the route refuses if the origin isn't in the agent's allow-list.

---

## Architecture

From the SDK's perspective, the `apiUrl` always points at the **Surogates API server** (the FastAPI process that serves `/v1/*`). The SDK never talks to workers directly:

```
Browser (SDK)
    │
    │  POST   /v1/website/sessions
    │  POST   /v1/website/sessions/{id}/messages
    │  GET    /v1/website/sessions/{id}/events   (SSE)
    │
    ▼
API server
    │  bootstrap  → insert sessions row + create Garage bucket + set cookie
    │  send       → emit user.message event + enqueue to Redis work queue
    │  SSE        → read events table + subscribe to Redis pub/sub nudges
    │
    ├──► PostgreSQL
    ├──► Redis       (queue + wake nudges)
    └──► Garage      (session + tenant buckets)

                    ┌─ pop session from Redis, run harness loop ─┐
                    │                                             │
Worker pod(s) ──────┘───────► PostgreSQL (appends llm.delta, tool.call, ...)
                                             │
                                             └─ Redis pub/sub nudges
                                                    │
                            API server SSE picks up and forwards to SDK
```

The API server is the trusted control plane; workers are the brain; sandboxes (per-session K8s pods) are the hands. Only the API server is network-reachable from the browser. See [VISION.md](../../VISION.md) for the motivation behind the decoupling.

## Security model

| Concern | Mitigation |
|---|---|
| Publishable key leak | Key authority is only recognised together with an `Origin` in the agent's allow-list. A key lifted from your site and used from a different origin is rejected at bootstrap. |
| Session cookie theft | Cookie is HttpOnly (inaccessible to JS) + Secure (HTTPS only) + SameSite=None (cross-site because the widget embed is cross-site by definition). The cookie's `origin` claim is re-verified on every request so replay from a different origin fails even if the attacker has the cookie. |
| Cross-site request forgery | Double-submit CSRF — the `X-CSRF-Token` header must match the `csrf` claim baked into the HttpOnly cookie. Cross-origin JS cannot read the cookie, so it cannot forge a matching header. |
| Over-privileged visitor | `tool_allow_list` materialises onto `session.config` at bootstrap; the harness enforces it before dispatch. A visitor session physically cannot invoke tools outside the list, no matter what the LLM generates. |
| Runaway visitor | `session_message_cap` and `session_token_cap` bound cost per session; `session_idle_minutes` triggers in-place reset without running the memory-flush agent (visitors have no per-user memory). |
| Ops-side disable | Calling `store.update(agent_id, enabled=False)` stops new bootstraps immediately and in-flight sessions within the auth cache TTL (~30s). |
| Cross-session replay | The session cookie JWT scopes to a single `session_id`; hitting `/v1/website/sessions/{other}/messages` with it returns 404 indistinguishable from "session doesn't exist". |
| Stolen key exfiltration via attacker site | Even with a valid key, the bootstrap fails unless `Origin` matches the agent's allow-list. Ops enumerates every permitted origin at provision time. |

## Interaction with other subsystems

* **Memory**: website sessions have `user_id=None`. The idle-reset job skips the memory-flush agent (no per-user memory to preserve) and resets in place with reason `idle_website_visitor`.
* **Training data**: every website session participates in `TrainingDataCollector` exports on the same footing as every other channel.
* **Prompt injection**: the global `PromptInjectionDetector` does **not** run on website messages yet; adding it is straightforward but would need a review of how anonymous-visitor input compares to authenticated-user input for false-positive rates.
* **Rate limiting**: per-token rate limits in `surogates:rate:*` do not apply to the website channel (the middleware keys off `Authorization`, which the browser doesn't carry after bootstrap). Consider per-IP limits at your edge/ingress for abuse protection.
* **Out of scope for v1**: hosted React components, visitor identity continuity across bootstraps (cookie expiry → new session), per-agent analytics dashboard, CAPTCHA. The [SDK README](../../sdk/website-widget/README.md) discusses follow-ups.
