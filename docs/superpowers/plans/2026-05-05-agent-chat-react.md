# Agent Chat React Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a reusable React chat package in the Surogates repo, migrate the standalone Surogates web chat to it, and wire Surogate Ops Work agent pages to live, agent-scoped chat sessions.

**Architecture:** `@invergent/agent-chat-react` owns the chat runtime reducer, live SSE interpretation, chat UI, tool rendering, artifact rendering, and clarify widgets. Each consuming app owns routing, authentication, API base URLs, workspace side panels, transparency banners, and an adapter that binds package calls to that app's HTTP/SSE endpoints. Ops live chat goes through new `/api/sessions/*` proxy routes that authorize the selected Work agent, then forward requests to the running agent's `api_url` with the existing service-account token lifecycle.

**Tech Stack:** React 19, TypeScript 5.9, Vite 8, Tailwind CSS 4, tsup, Vitest, FastAPI, httpx, SQLAlchemy, Surogates session/event APIs.

---

## Progress Todo

- [x] Commit 1: Add progress tracking to this plan.
- [x] Commit 2: Scaffold `@invergent/agent-chat-react` package.
- [x] Commit 3: Extract and test the pure chat event reducer.
- [x] Commit 4: Implement adapter-driven chat runtime hook.
- [x] Commit 5: Move reusable chat UI into the package.
- [x] Commit 6: Migrate standalone `/work/surogates/web` chat to the package.
- [x] Commit 7: Add Ops backend live chat proxy routes.
- [x] Commit 8: Wire Ops Work chat routes and navbar.
- [x] Commit 9: Complete cross-repo verification fixes.
- [x] Commit 10: Restore standalone login visibility styles.
- [x] Commit 11: Refactor login page to use shared UI components.
- [x] Commit 12: Publish SDK packages from `v*` tags via GitHub Packages.
- [x] Commit 13: Allow SDK package versions to publish independently from repo release tags.
- [x] Commit 14: Make Docker image publication wait for npm package publication.
- [x] Commit 15: Copy the local agent chat SDK into the API image web build stage.
- [x] Commit 16: Restore full legacy chat UI behavior behind the SDK adapter contract.
- [x] Commit 17: Resolve API image SDK source dependencies through the web build install.
- [x] Commit 18: Fix Ops package resolution and selected-agent session scoping.
- [x] Commit 19: Add service-account live-chat endpoints in Surogates.
- [x] Commit 20: Point Ops live-chat proxy at the service-account endpoints.

---

## Code Review Findings

- `/work/surogates/pnpm-workspace.yaml` only includes `sdk/*`; `/work/surogates/web` is deliberately not a workspace package. The shared package should live in `/work/surogates/sdk/agent-chat-react`, and both `web` and `/work/surogate-ops/frontend` should consume it with a local `file:` dependency during development.
- `sdk/website-widget` is the closest package template: `package.json`, `tsconfig.json`, `tsup.config.ts`, `vitest.config.ts`, `src/index.ts`, and `tests/*.test.ts`.
- The reusable chat surface currently spans:
  - `web/src/hooks/use-session-runtime.ts`
  - `web/src/components/chat/**`
  - `web/src/components/ai-elements/{artifact,conversation,message,reasoning,shimmer}.tsx`
  - `web/src/components/reui/timeline.tsx`
  - UI primitives used by those files under `web/src/components/ui/**`
  - `web/src/types/session.ts`
  - artifact and clarify API calls in `web/src/api/artifacts.ts` and `web/src/api/clarify.ts`
- The current runtime hook hardcodes standalone concerns: `getAuthToken()`, `/api/v1/sessions/{id}/events`, `getSession()`, and `retrySession()`. Extract a pure event reducer first, then wrap it in an adapter-driven hook.
- Tool renderers are not fully app-independent. `ClarifyToolBlock` uses `useAppStore`, `submitClarifyResponse`, and `pauseSession`; `ArtifactBlock` imports `getArtifact`. These must move behind package context/adapter methods before the package can be reused by Ops.
- `ChatPage` owns standalone-only behavior: TanStack routes, Zustand session store, EU AI Act transparency banner, workspace panel, and URL synchronization. Keep those in `/work/surogates/web`; only replace chat internals with `AgentChat`.
- Ops already has read-only sessions APIs and types:
  - `/work/surogate-ops/surogate_ops/server/routes/sessions.py`
  - `/work/surogate-ops/surogate_ops/core/surogates_client.py`
  - `/work/surogate-ops/frontend/src/api/sessions.ts`
  - `/work/surogate-ops/frontend/src/types/session.ts`
- Ops Work agent navigation currently uses mock sessions in `/work/surogate-ops/frontend/src/features/work/work-agent-navbar.tsx`. Replace `getMockAgentSessions(agentId)` with `listSessions({ agentId })` and route links.
- Ops agent records already carry `Agent.status` and `Agent.api_url` in `/work/surogate-ops/surogate_ops/core/db/models/operate.py`; new live-chat proxy routes should reject missing or non-running agents before forwarding.
- Ops already has the service-account/token-refresh pattern in `/work/surogate-ops/surogate_ops/core/synthetic/surogates_api.py`. Reuse that behavior by extending `SurogatesApiClient` with generic session request helpers or by introducing a small `AgentChatProxyClient` that delegates token management to the same code.

## File Structure

### New Package

- Create `/work/surogates/sdk/agent-chat-react/package.json`: package metadata, `tsup`, `vitest`, React peer dependencies, direct internal UI dependencies.
- Create `/work/surogates/sdk/agent-chat-react/tsconfig.json`: strict package TypeScript settings, DOM libs, declaration output.
- Create `/work/surogates/sdk/agent-chat-react/tsup.config.ts`: ESM/CJS build, dts output, external React peers.
- Create `/work/surogates/sdk/agent-chat-react/vitest.config.ts`: `happy-dom` unit-test environment.
- Create `/work/surogates/sdk/agent-chat-react/src/index.ts`: public exports.
- Create `/work/surogates/sdk/agent-chat-react/src/types.ts`: package-owned session, event, artifact, tool, adapter, and runtime types.
- Create `/work/surogates/sdk/agent-chat-react/src/runtime/events.ts`: listened event names and SSE parser helpers.
- Create `/work/surogates/sdk/agent-chat-react/src/runtime/reducer.ts`: pure event-to-chat-state reducer extracted from `use-session-runtime.ts`.
- Create `/work/surogates/sdk/agent-chat-react/src/runtime/use-agent-chat-runtime.ts`: React hook that uses the adapter, reducer, optimistic send, stop, retry, and EventSource lifecycle.
- Create `/work/surogates/sdk/agent-chat-react/src/adapter-context.tsx`: context used by nested renderers for artifact, clarify, pause, and file callbacks.
- Create `/work/surogates/sdk/agent-chat-react/src/agent-chat.tsx`: top-level `AgentChat` component.
- Create `/work/surogates/sdk/agent-chat-react/src/components/**`: copied and alias-adjusted chat, ai-elements, reui, and UI primitives needed by the package.
- Create `/work/surogates/sdk/agent-chat-react/tests/reducer.test.ts`: reducer coverage for replay/live event behavior.
- Create `/work/surogates/sdk/agent-chat-react/tests/runtime.test.tsx`: hook/component integration tests with fake adapter and fake EventSource.

### Standalone Web Changes

- Modify `/work/surogates/web/package.json`: add `"@invergent/agent-chat-react": "file:../sdk/agent-chat-react"`.
- Modify `/work/surogates/web/vite.config.ts`: dedupe React and allow package source/dependency build behavior.
- Create `/work/surogates/web/src/features/chat/surogates-web-chat-adapter.ts`: standalone adapter for `/api/v1/sessions`.
- Modify `/work/surogates/web/src/features/chat/chat-page.tsx`: render `AgentChat` and keep route/session/transparency/workspace concerns local.
- Retire or stop importing migrated local chat files only after web typecheck/build passes.

### Ops Backend Changes

- Modify `/work/surogate-ops/surogate_ops/core/synthetic/surogates_api.py`: add generic JSON request and streaming request support, or extract shared token lifecycle into a reusable helper.
- Modify `/work/surogate-ops/surogate_ops/server/routes/sessions.py`: add live mutating routes while preserving existing read-only audit routes.
- Add `/work/surogate-ops/tests/test_sessions_live_proxy.py`: route-level tests for tenant checks, stopped agent rejection, forwarding, and 401 token refresh.

### Ops Frontend Changes

- Modify `/work/surogate-ops/frontend/package.json`: add `"@invergent/agent-chat-react": "file:../../surogates/sdk/agent-chat-react"`.
- Modify `/work/surogate-ops/frontend/vite.config.ts`: dedupe React and include the local package for optimization as needed.
- Create `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`: Ops adapter for `/api/sessions`.
- Create `/work/surogate-ops/frontend/src/features/work/work-agent-chat-page.tsx`: Work chat route component.
- Modify `/work/surogate-ops/frontend/src/app/routes/work.tsx`: add `/work/agents/$agentId/chat` and `/work/agents/$agentId/chat/$sessionId`.
- Modify `/work/surogate-ops/frontend/src/app/router.tsx`: attach the new Work chat child routes.
- Modify `/work/surogate-ops/frontend/src/features/work/work-agent-navbar.tsx`: replace mock sessions with live agent-scoped sessions and New chat navigation.
- Modify `/work/surogate-ops/frontend/src/api/sessions.ts`: add live create/send/pause/retry/stream helpers or keep those details in `work-agent-chat-adapter.ts`.

---

## Adapter Contract

Use this as the package public contract. Field names are camelCase inside React code; app adapters translate wire formats.

```ts
export interface AgentChatAdapter {
  listSessions(input: {
    agentId?: string;
    limit?: number;
    offset?: number;
  }): Promise<AgentChatSessionList>;
  createSession(input: {
    agentId?: string;
    system?: string;
  }): Promise<AgentChatSession>;
  getSession(input: { sessionId: string }): Promise<AgentChatSession>;
  sendMessage(input: {
    sessionId: string;
    content: string;
  }): Promise<{ eventId?: number; status?: string }>;
  pauseSession(input: { sessionId: string }): Promise<void>;
  retrySession(input: { sessionId: string }): Promise<AgentChatSession>;
  deleteSession?(input: { sessionId: string }): Promise<void>;
  getArtifact(input: {
    sessionId: string;
    artifactId: string;
  }): Promise<AgentChatArtifactPayload>;
  submitClarifyResponse(input: {
    sessionId: string;
    toolCallId: string;
    responses: AgentChatClarifyAnswer[];
  }): Promise<{ eventId?: number }>;
  openEventStream(input: {
    sessionId: string;
    after: number;
  }): AgentChatEventStream;
}

export interface AgentChatEventStream {
  addEventListener(
    type: AgentChatEventType,
    listener: (event: AgentChatSseMessageEvent) => void,
  ): void;
  close(): void;
  onerror: (() => void) | null;
}
```

`AgentChat` entry point:

```tsx
<AgentChat
  agentId={agentId}
  sessionId={sessionId ?? null}
  adapter={adapter}
  onSessionChange={setSessionId}
  onFileSelect={handleFileSelect}
/>
```

---

## Task 1: Scaffold `@invergent/agent-chat-react`

**Files:**
- Create: `/work/surogates/sdk/agent-chat-react/package.json`
- Create: `/work/surogates/sdk/agent-chat-react/tsconfig.json`
- Create: `/work/surogates/sdk/agent-chat-react/tsup.config.ts`
- Create: `/work/surogates/sdk/agent-chat-react/vitest.config.ts`
- Create: `/work/surogates/sdk/agent-chat-react/src/index.ts`
- Create: `/work/surogates/sdk/agent-chat-react/src/types.ts`

- [ ] **Step 1: Create package metadata**

Use this dependency split: `react` and `react-dom` are peers; renderer libraries used internally by chat are package dependencies so both apps do not need to hand-maintain the same list.

```json
{
  "name": "@invergent/agent-chat-react",
  "version": "0.1.0",
  "description": "Reusable React chat UI and runtime for Surogates agent sessions.",
  "license": "AGPL-3.0-only",
  "type": "module",
  "main": "./dist/index.cjs",
  "module": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "import": "./dist/index.js",
      "require": "./dist/index.cjs"
    },
    "./package.json": "./package.json"
  },
  "files": ["dist", "README.md", "LICENSE"],
  "sideEffects": false,
  "engines": { "node": ">=20" },
  "scripts": {
    "build": "tsup",
    "dev": "tsup --watch",
    "test": "vitest run",
    "test:watch": "vitest",
    "typecheck": "tsc --noEmit",
    "prepublishOnly": "pnpm run typecheck && pnpm run test && pnpm run build"
  },
  "peerDependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "dependencies": {
    "@radix-ui/react-checkbox": "^1.3.3",
    "@radix-ui/react-label": "^2.1.8",
    "@radix-ui/react-select": "^2.2.6",
    "@radix-ui/react-separator": "^1.1.8",
    "@radix-ui/react-slot": "^1.2.4",
    "ansi-to-react": "^6.2.6",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "cmdk": "^1.1.1",
    "date-fns": "^4.1.0",
    "diff": "^8.0.4",
    "lucide-react": "^1.8.0",
    "radix-ui": "^1.4.3",
    "react-vega": "^8.0.0",
    "recharts": "^3.8.0",
    "streamdown": "2.5.0",
    "tailwind-merge": "^3.5.0",
    "tw-shimmer": "^0.4.6",
    "vega": "^6.2.0",
    "vega-lite": "^6.4.2"
  },
  "devDependencies": {
    "@types/node": "^24.10.1",
    "@types/react": "^19.2.5",
    "@types/react-dom": "^19.2.3",
    "happy-dom": "^15.11.0",
    "tsup": "^8.3.5",
    "typescript": "~5.9.3",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: Create TypeScript and build config**

Copy the strict settings from `sdk/website-widget/tsconfig.json`, with `types: ["node", "react", "react-dom"]`.

Use this `tsup.config.ts`:

```ts
import { defineConfig } from "tsup";

export default defineConfig({
  entry: { index: "src/index.ts" },
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  treeshake: true,
  target: "es2020",
  outDir: "dist",
  external: ["react", "react-dom", "react/jsx-runtime"],
});
```

- [ ] **Step 3: Add the initial public type surface**

Create `src/types.ts` with the adapter contract above plus package-owned copies of the existing `Session`, `ChatMessage`, `ToolCallInfo`, `TokenUsage`, `RetryIndicator`, `ErrorInfo`, artifact, and clarify types. Keep the package names prefixed, for example `AgentChatSession`, so consumer apps can map their own API types cleanly.

- [ ] **Step 4: Export only stable public APIs**

Create `src/index.ts`:

```ts
export { AgentChat } from "./agent-chat";
export { useAgentChatRuntime } from "./runtime/use-agent-chat-runtime";
export type {
  AgentChatAdapter,
  AgentChatArtifactKind,
  AgentChatArtifactPayload,
  AgentChatClarifyAnswer,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatMessage,
  AgentChatSession,
  AgentChatSessionList,
  AgentChatSseMessageEvent,
  AgentChatState,
  AgentChatToolCallInfo,
} from "./types";
```

- [ ] **Step 5: Verify scaffold**

Run:

```bash
pnpm --dir /work/surogates/sdk/agent-chat-react typecheck
pnpm --dir /work/surogates/sdk/agent-chat-react build
```

Expected: both commands pass before any consumer app imports the package.

---

## Task 2: Extract And Test The Chat Runtime Reducer

**Files:**
- Create: `/work/surogates/sdk/agent-chat-react/src/runtime/events.ts`
- Create: `/work/surogates/sdk/agent-chat-react/src/runtime/reducer.ts`
- Create: `/work/surogates/sdk/agent-chat-react/tests/reducer.test.ts`
- Read source: `/work/surogates/web/src/hooks/use-session-runtime.ts`

- [ ] **Step 1: Move event constants into package runtime**

Create `runtime/events.ts` with the exact `LISTENED_EVENTS` currently in `use-session-runtime.ts`, including `session.done`, `stream.timeout`, `expert.endorse`, `artifact.updated`, and `clarify.response`.

- [ ] **Step 2: Convert reducer logic to a pure function**

Extract `applyEvent` behavior into:

```ts
export function applyAgentChatEvent(
  state: AgentChatState,
  event: AgentChatRuntimeEvent,
): AgentChatState
```

The returned state must include:

```ts
{
  messages: AgentChatMessage[];
  isRunning: boolean;
  tokenUsage: AgentChatTokenUsage;
  retryIndicator: AgentChatRetryIndicator | null;
  lastEventId: number;
  sessionDone: boolean;
  hadDeltas: boolean;
  terminal: boolean;
}
```

Keep helper functions from the hook (`findLastAssistantIndex`, `hasUserAfterIndex`, `findLatestConsultExpertCall`) in the reducer module.

- [ ] **Step 3: Write failing reducer tests**

Add tests for:

```ts
it("reconciles optimistic user messages with authoritative user.message events", () => {})
it("does not duplicate llm.response content after llm.delta streamed it", () => {})
it("keeps running true across harness.crash and exposes retry indicator", () => {})
it("marks session.fail as terminal and inserts a standalone error when no assistant slot exists", () => {})
it("attaches artifact metadata as a system timeline message", () => {})
it("attaches clarify.response answers to the matching tool call", () => {})
```

- [ ] **Step 4: Run tests to verify they fail for missing implementation**

Run:

```bash
pnpm --dir /work/surogates/sdk/agent-chat-react test -- reducer.test.ts
```

Expected: failures identify missing exported reducer/types before implementation is complete.

- [ ] **Step 5: Implement reducer until tests pass**

Port the switch cases from `/work/surogates/web/src/hooks/use-session-runtime.ts` without importing app APIs. Any state previously stored in refs becomes explicit `AgentChatState` fields.

- [ ] **Step 6: Verify runtime reducer**

Run:

```bash
pnpm --dir /work/surogates/sdk/agent-chat-react test -- reducer.test.ts
pnpm --dir /work/surogates/sdk/agent-chat-react typecheck
```

Expected: reducer tests and package typecheck pass.

---

## Task 3: Build Adapter-Driven Runtime Hook

**Files:**
- Create: `/work/surogates/sdk/agent-chat-react/src/runtime/use-agent-chat-runtime.ts`
- Create: `/work/surogates/sdk/agent-chat-react/tests/runtime.test.tsx`
- Modify: `/work/surogates/sdk/agent-chat-react/src/types.ts`

- [ ] **Step 1: Write hook tests with a fake adapter**

Cover:

```ts
it("opens the adapter event stream with the current session id and cursor", () => {})
it("closes the old stream when session id changes", () => {})
it("optimistically appends a user message before sendMessage resolves", () => {})
it("creates a session before sending when sessionId is null", () => {})
it("calls pauseSession and marks the current stream stopped on stop", () => {})
it("calls retrySession and clears terminal state on retry", () => {})
```

- [ ] **Step 2: Implement `useAgentChatRuntime`**

Signature:

```ts
export function useAgentChatRuntime(input: {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
}): AgentChatRuntimeApi
```

The hook must:

- use `adapter.openEventStream({ sessionId, after: state.lastEventId })`
- parse `event.data` JSON and `event.lastEventId`
- dispatch events through `applyAgentChatEvent`
- call `adapter.getSession({ sessionId })` after connecting to set `terminal`
- reconnect after `onerror` unless `sessionDone` is true
- expose `send`, `stop`, `retry`, `markSending`, `markSendError`, `messages`, `isRunning`, `tokenUsage`, and `retryIndicator`

- [ ] **Step 3: Verify hook**

Run:

```bash
pnpm --dir /work/surogates/sdk/agent-chat-react test -- runtime.test.tsx
pnpm --dir /work/surogates/sdk/agent-chat-react typecheck
```

Expected: hook tests and package typecheck pass.

---

## Task 4: Move Chat UI Into The Package

**Files:**
- Create/modify: `/work/surogates/sdk/agent-chat-react/src/agent-chat.tsx`
- Create/modify: `/work/surogates/sdk/agent-chat-react/src/adapter-context.tsx`
- Create: `/work/surogates/sdk/agent-chat-react/src/components/chat/**`
- Create: `/work/surogates/sdk/agent-chat-react/src/components/ai-elements/**`
- Create: `/work/surogates/sdk/agent-chat-react/src/components/reui/timeline.tsx`
- Create: `/work/surogates/sdk/agent-chat-react/src/components/ui/**`
- Read source: `/work/surogates/web/src/components/chat/**`

- [ ] **Step 1: Copy the chat renderer tree**

Copy the currently used files from `web/src/components/chat/**`, `web/src/components/ai-elements/{artifact,conversation,message,reasoning,shimmer,code-block}.tsx`, `web/src/components/reui/timeline.tsx`, and required UI primitives into the package.

After copying, replace imports such as `@/components/chat/chat-thread` with relative package imports. For example:

```ts
import { cn } from "../../lib/utils";
import type { AgentChatMessage } from "../../types";
```

- [ ] **Step 2: Replace app store/API imports with package context**

`ArtifactBlock` must call:

```ts
const { adapter } = useAgentChatAdapterContext();
const payload = await adapter.getArtifact({ sessionId, artifactId });
```

`ClarifyToolBlock` must call:

```ts
const { adapter, sessionId } = useAgentChatAdapterContext();
await adapter.submitClarifyResponse({ sessionId, toolCallId: tc.id, responses });
await adapter.pauseSession({ sessionId });
```

No package file may import from `/work/surogates/web/src/api/*`, `/work/surogates/web/src/stores/*`, or `@/features/auth`.

- [ ] **Step 3: Implement the top-level component**

`AgentChat` should:

```tsx
export function AgentChat(props: AgentChatProps) {
  const runtime = useAgentChatRuntime({
    adapter: props.adapter,
    agentId: props.agentId,
    sessionId: props.sessionId,
    onSessionChange: props.onSessionChange,
  });

  return (
    <AgentChatAdapterProvider
      adapter={props.adapter}
      sessionId={props.sessionId}
      onFileSelect={props.onFileSelect}
    >
      <ChatThread
        sessionId={props.sessionId}
        messages={runtime.messages}
        isRunning={runtime.isRunning}
        onSend={runtime.send}
        onStop={runtime.stop}
        onFileSelect={props.onFileSelect}
        disabled={props.disabled}
        tokenUsage={runtime.tokenUsage}
        retryIndicator={runtime.retryIndicator}
        onRetry={runtime.retry}
      />
    </AgentChatAdapterProvider>
  );
}
```

- [ ] **Step 4: Verify no app-private imports remain**

Run:

```bash
rg -n '"@/|web/src|useAppStore|getAuthToken|authFetch|/api/v1' /work/surogates/sdk/agent-chat-react/src
pnpm --dir /work/surogates/sdk/agent-chat-react typecheck
pnpm --dir /work/surogates/sdk/agent-chat-react build
```

Expected: `rg` returns no matches for app-private imports or hardcoded standalone API paths; typecheck/build pass.

---

## Task 5: Migrate Standalone Web To The Package

**Files:**
- Modify: `/work/surogates/web/package.json`
- Modify: `/work/surogates/web/vite.config.ts`
- Create: `/work/surogates/web/src/features/chat/surogates-web-chat-adapter.ts`
- Modify: `/work/surogates/web/src/features/chat/chat-page.tsx`

- [ ] **Step 1: Add the local package dependency**

Add to `/work/surogates/web/package.json` dependencies:

```json
"@invergent/agent-chat-react": "file:../sdk/agent-chat-react"
```

Run:

```bash
npm --prefix /work/surogates/web install
```

Expected: `package-lock.json` updates with the local package.

- [ ] **Step 2: Create standalone adapter**

Create `surogates-web-chat-adapter.ts` using `authFetch`, `getAuthToken`, and existing standalone wire endpoints:

```ts
export const surogatesWebChatAdapter: AgentChatAdapter = {
  async listSessions(input) {
    const res = await sessionsApi.listSessions({
      limit: input.limit,
      offset: input.offset,
    });
    return { sessions: res.sessions.map(toAgentChatSession), total: res.total };
  },
  async createSession(input) {
    return toAgentChatSession(await sessionsApi.createSession({ system: input.system }));
  },
  async getSession(input) {
    return toAgentChatSession(await sessionsApi.getSession(input.sessionId));
  },
  async sendMessage(input) {
    const res = await sessionsApi.sendMessage(input.sessionId, input.content);
    return { eventId: res.event_id, status: res.status };
  },
  async pauseSession(input) {
    await sessionsApi.pauseSession(input.sessionId);
  },
  async retrySession(input) {
    return toAgentChatSession(await sessionsApi.retrySession(input.sessionId));
  },
  async deleteSession(input) {
    await sessionsApi.deleteSession(input.sessionId);
  },
  async getArtifact(input) {
    return await getArtifact(input.sessionId, input.artifactId);
  },
  async submitClarifyResponse(input) {
    const res = await submitClarifyResponse(input.sessionId, input.toolCallId, input.responses);
    return { eventId: res.event_id };
  },
  openEventStream(input) {
    const token = getAuthToken();
    const url = new URL(`/api/v1/sessions/${input.sessionId}/events`, window.location.origin);
    url.searchParams.set("after", String(input.after));
    if (token) url.searchParams.set("token", token);
    return new EventSource(url.toString());
  },
};
```

- [ ] **Step 3: Replace `ChatThread` and `useSessionRuntime` in `ChatPage`**

Keep session URL synchronization, `SessionSidebar`, `WorkspacePanel`, transparency banner, disclosure state, and workspace file selection in `chat-page.tsx`.

Replace the local runtime/render call with:

```tsx
<AgentChat
  sessionId={sessionId ?? null}
  adapter={surogatesWebChatAdapter}
  onSessionChange={(nextSessionId) => {
    setActiveSession(nextSessionId);
    void fetchSessions();
    void navigate({
      to: "/chat/$sessionId",
      params: { sessionId: nextSessionId },
    });
  }}
  onFileSelect={handleFileSelect}
  disabled={sessionDeclined}
/>
```

If transparency pre-session acceptance still needs `confirmDisclosure`, keep that call in `onSessionChange` or introduce an optional post-create wrapper around the adapter inside `ChatPage`.

- [ ] **Step 4: Verify standalone web**

Run:

```bash
npm --prefix /work/surogates/web run typecheck
npm --prefix /work/surogates/web run build
```

Expected: both pass and `/chat` plus `/chat/:sessionId` still route through the standalone app shell.

---

## Task 6: Add Ops Live Chat Proxy Routes

**Files:**
- Modify: `/work/surogate-ops/surogate_ops/core/synthetic/surogates_api.py`
- Modify: `/work/surogate-ops/surogate_ops/server/routes/sessions.py`
- Add: `/work/surogate-ops/tests/test_sessions_live_proxy.py`

- [ ] **Step 1: Add request helpers that reuse service-account token lifecycle**

Extend `SurogatesApiClient` with:

```py
async def request_json(
    self,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
) -> tuple[int, dict]:
    resp = await self._request(method, path, json_body=json_body)
    if resp.content:
        return resp.status_code, resp.json()
    return resp.status_code, {}
```

For SSE proxying, add a streaming method that returns an `httpx.Response` context manager from the same authenticated client and follows the same one-time 401 invalidation behavior.

- [ ] **Step 2: Add request/response models to Ops sessions route**

In `routes/sessions.py` add:

```py
class LiveCreateSessionRequest(BaseModel):
    agent_id: str
    system: str | None = None

class LiveSendMessageRequest(BaseModel):
    content: str

class LiveSessionResponse(BaseModel):
    id: UUID
    status: str
    channel: str | None = None
    model: str | None = None
```

- [ ] **Step 3: Resolve a live agent target**

Add helper:

```py
async def _resolve_live_agent(
    agent_id: str,
    ops_session: AsyncSession,
    surogates: SurogatesClient,
):
    org_id = await _resolve_agent_org(agent_id, ops_session, surogates)
    agent = await agent_repo.get_agent(ops_session, agent_id)
    if agent is None or agent.project is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status.value != "running" or not agent.api_url:
        raise HTTPException(status_code=409, detail="Agent is not running")
    return agent, org_id
```

Also add a session-scoped helper:

```py
async def _resolve_live_session_agent(
    session_id: UUID,
    ops_session: AsyncSession,
    surogates: SurogatesClient,
):
    owner = await surogates.get_session_owner(session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found")
    agent_id, session_org_id = owner
    agent = await agent_repo.get_agent(ops_session, agent_id)
    if agent is None or agent.project is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if UUID(agent.project.id) != session_org_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if agent.status.value != "running" or not agent.api_url:
        raise HTTPException(status_code=409, detail="Agent is not running")
    return agent
```

- [ ] **Step 4: Add mutating live routes**

Add:

```py
@router.post("")
async def create_live_session(
    body: LiveCreateSessionRequest,
    request: Request,
    ops_session: AsyncSession = Depends(get_session),
    current_subject: str = Depends(get_current_subject),
):
    surogates: SurogatesClient = request.app.state.surogates
    agent, org_id = await _resolve_live_agent(body.agent_id, ops_session, surogates)
    client = _agent_chat_proxy_client(agent.api_url, org_id, surogates)
    status_code, payload = await client.request_json(
        "POST",
        "/v1/sessions",
        json_body={"system": body.system} if body.system else {},
    )
    return JSONResponse(status_code=status_code, content=payload)

@router.post("/{session_id}/messages")
async def send_live_message(
    body: LiveSendMessageRequest,
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    agent = await _resolve_live_session_agent(scope.session_id, ops_session, scope.surogates)
    client = _agent_chat_proxy_client(agent.api_url, scope.org_id, scope.surogates)
    status_code, payload = await client.request_json(
        "POST",
        f"/v1/sessions/{scope.session_id}/messages",
        json_body={"content": body.content},
    )
    return JSONResponse(status_code=status_code, content=payload)

@router.get("/{session_id}/events/stream")
async def stream_live_events(
    after: int = Query(0, ge=0),
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    agent = await _resolve_live_session_agent(scope.session_id, ops_session, scope.surogates)
    return await _proxy_agent_sse(agent, scope.org_id, scope.surogates, scope.session_id, after)

@router.post("/{session_id}/pause")
async def pause_live_session(
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    agent = await _resolve_live_session_agent(scope.session_id, ops_session, scope.surogates)
    client = _agent_chat_proxy_client(agent.api_url, scope.org_id, scope.surogates)
    status_code, payload = await client.request_json(
        "POST",
        f"/v1/sessions/{scope.session_id}/pause",
    )
    return JSONResponse(status_code=status_code, content=payload)

@router.post("/{session_id}/retry")
async def retry_live_session(
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    agent = await _resolve_live_session_agent(scope.session_id, ops_session, scope.surogates)
    client = _agent_chat_proxy_client(agent.api_url, scope.org_id, scope.surogates)
    status_code, payload = await client.request_json(
        "POST",
        f"/v1/sessions/{scope.session_id}/retry",
    )
    return JSONResponse(status_code=status_code, content=payload)
```

`POST /api/sessions` accepts `{ "agent_id": "agent-123", "system": "You are concise." }` and proxies to `POST /v1/sessions`.

Session-scoped mutating routes must first call `resolve_session_scope`; then load the owning ops agent and verify it is running with `api_url`.

The stream route proxies to:

```txt
GET {agent.api_url}/v1/sessions/{session_id}/events?after={after}
```

and returns `StreamingResponse` with `media_type="text/event-stream"`.

Because browser `EventSource` cannot set an `Authorization` header, the Ops stream route must accept the JWT from a `token` query parameter. Mirror the Surogates auth middleware behavior: use the regular `Authorization: Bearer` path for tests and non-SSE clients, but for `events/stream` validate `?token=` through the same auth function before resolving the session scope.

- [ ] **Step 5: Test backend behavior**

Tests should assert:

```py
async def test_create_live_session_requires_owned_agent():
    response = await client.post(
        "/api/sessions",
        json={"agent_id": "agent-from-another-project"},
        headers=auth_headers,
    )
    assert response.status_code == 404

async def test_create_live_session_rejects_stopped_agent():
    response = await client.post(
        "/api/sessions",
        json={"agent_id": stopped_agent.id},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Agent is not running"

async def test_send_live_message_resolves_session_scope_before_proxy():
    response = await client.post(
        f"/api/sessions/{foreign_session_id}/messages",
        json={"content": "hello"},
        headers=auth_headers,
    )
    assert response.status_code == 404

async def test_stream_live_events_uses_text_event_stream():
    response = await client.get(
        f"/api/sessions/{owned_session_id}/events/stream?after=0",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

async def test_proxy_refreshes_service_account_token_once_on_401():
    fake_agent_api.queue_response(401, {"detail": "invalid token"})
    fake_agent_api.queue_response(201, {"id": str(owned_session_id), "status": "active"})
    response = await client.post(
        "/api/sessions",
        json={"agent_id": running_agent.id},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert surogates.create_service_account_call_count == 1
```

Run:

```bash
pytest /work/surogate-ops/tests/test_sessions_live_proxy.py -q
```

Expected: tests pass without weakening existing read-only session route behavior.

---

## Task 7: Wire Ops Frontend Work Chat

**Files:**
- Modify: `/work/surogate-ops/frontend/package.json`
- Modify: `/work/surogate-ops/frontend/vite.config.ts`
- Create: `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`
- Create: `/work/surogate-ops/frontend/src/features/work/work-agent-chat-page.tsx`
- Modify: `/work/surogate-ops/frontend/src/app/routes/work.tsx`
- Modify: `/work/surogate-ops/frontend/src/app/router.tsx`
- Modify: `/work/surogate-ops/frontend/src/features/work/work-agent-navbar.tsx`

- [ ] **Step 1: Add package dependency**

Add to `/work/surogate-ops/frontend/package.json` dependencies:

```json
"@invergent/agent-chat-react": "file:../../surogates/sdk/agent-chat-react"
```

Run:

```bash
npm --prefix /work/surogate-ops/frontend install
```

Expected: `package-lock.json` updates with the local package.

- [ ] **Step 2: Create Ops adapter**

Use `authFetch` from `/work/surogate-ops/frontend/src/api/auth.ts`.

`openEventStream` must include the bearer token in a query param because browser `EventSource` cannot send custom headers. The route should be:

```ts
const token = getAuthToken();
const url = new URL(`/api/sessions/${sessionId}/events/stream`, window.location.origin);
url.searchParams.set("after", String(after));
if (token) url.searchParams.set("token", token);
return new EventSource(url.toString());
```

For create:

```ts
await authFetch("/api/sessions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ agent_id: agentId, system }),
});
```

- [ ] **Step 3: Add Work chat page**

Create:

```tsx
export function WorkAgentChatPage() {
  const navigate = useNavigate();
  const { agentId = "", sessionId } = useParams({ strict: false }) as {
    agentId?: string;
    sessionId?: string;
  };
  const adapter = useMemo(() => createWorkAgentChatAdapter(agentId), [agentId]);

  return (
    <AgentChat
      agentId={agentId}
      sessionId={sessionId ?? null}
      adapter={adapter}
      onSessionChange={(nextSessionId) => {
        void navigate({
          to: "/work/agents/$agentId/chat/$sessionId",
          params: { agentId, sessionId: nextSessionId },
        });
      }}
    />
  );
}
```

- [ ] **Step 4: Add routes**

In `routes/work.tsx`, add lazy import and routes:

```ts
export const workAgentChatRoute = createRoute({
  getParentRoute: () => workAgentRoute,
  path: "/chat",
  component: WorkAgentChatPage,
});

export const workAgentChatSessionRoute = createRoute({
  getParentRoute: () => workAgentRoute,
  path: "/chat/$sessionId",
  component: WorkAgentChatPage,
});
```

In `router.tsx`, attach both routes to `workAgentRoute.addChildren([workAgentOverviewRoute, workAgentSettingsRoute, workAgentChatRoute, workAgentChatSessionRoute])`.

- [ ] **Step 5: Replace mock navbar sessions**

In `work-agent-navbar.tsx`:

- remove `getMockAgentSessions`
- fetch `listSessions({ agentId, status: "all", limit: 50 })`
- make New chat navigate to `/work/agents/$agentId/chat`
- make each session link navigate to `/work/agents/$agentId/chat/$sessionId`
- keep Overview and Configure active-state logic

- [ ] **Step 6: Verify Ops frontend**

Run:

```bash
npm --prefix /work/surogate-ops/frontend run typecheck
npm --prefix /work/surogate-ops/frontend run build
```

Expected: both pass; Work agent pages compile with the new package and routes.

---

## Task 8: Final Cross-Repo Verification

**Files:**
- Verify all changed files from Tasks 1-7.

- [ ] **Step 1: Package verification**

Run:

```bash
pnpm --dir /work/surogates/sdk/agent-chat-react test
pnpm --dir /work/surogates/sdk/agent-chat-react typecheck
pnpm --dir /work/surogates/sdk/agent-chat-react build
```

Expected: all pass.

- [ ] **Step 2: Standalone Surogates verification**

Run:

```bash
npm --prefix /work/surogates/web run typecheck
npm --prefix /work/surogates/web run build
```

Expected: all pass.

- [ ] **Step 3: Ops backend verification**

Run:

```bash
pytest /work/surogate-ops/tests/test_sessions_live_proxy.py -q
```

Expected: all pass.

- [ ] **Step 4: Ops frontend verification**

Run:

```bash
npm --prefix /work/surogate-ops/frontend run typecheck
npm --prefix /work/surogate-ops/frontend run build
```

Expected: all pass.

- [ ] **Step 5: Manual smoke checks**

Run both frontend dev servers and check:

- standalone `/chat` can create a session and stream responses
- standalone `/chat/:sessionId` can replay an existing session
- Work `/work/agents/:agentId/chat` opens an empty chat for the selected agent
- Work New chat creates a session scoped to that `agentId`
- Work `/work/agents/:agentId/chat/:sessionId` streams through Ops SSE proxy
- stopped Work agents show a non-2xx error instead of creating sessions
- artifact and clarify tool UI still work in standalone chat

---

## Implementation Notes

- Keep React deduped. If either app sees invalid hook call errors, add `resolve.dedupe: ["react", "react-dom"]` to its Vite config.
- Keep the shared package free of routing assumptions. All `navigate`, TanStack route params, sidebar state, transparency disclosure state, and workspace panel behavior stay in the consuming apps.
- Do not delete old `/work/surogates/web/src/components/chat/**` files until the package-backed standalone app has passed typecheck/build. Removing them can be a final cleanup task after both consumers compile.
- Ops backend route ordering matters: keep `@router.get("")` and `@router.post("")` together, and make sure `/{session_id}/events/stream` is declared before broad dynamic routes if FastAPI matching becomes ambiguous.
- The package should not own Tailwind setup. Consumers compile Tailwind classes from dependency source/build output through their normal Vite/Tailwind pipeline.
