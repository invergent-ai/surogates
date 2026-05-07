# Example Chat App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `sdk/example-chat-app`, a runnable Vite + Express example that uses `@invergent/agent-chat-react` with a real OpenAI-compatible `/v1/chat/completions` streaming backend.

**Architecture:** The package contains a React frontend, an adapter that implements the `AgentChatAdapter` interface over local REST/SSE endpoints, and a small Express backend with in-memory sessions, events, artifacts, and workspace files. Normal chat streams from a real provider; slash commands emit scripted events to exercise the rest of the UI surface.

**Tech Stack:** TypeScript, React 19, Vite, Express 5, Vitest, happy-dom, pnpm workspace dependencies.

---

## File Structure

- Create `sdk/example-chat-app/package.json`: package scripts and dependencies.
- Create `sdk/example-chat-app/tsconfig.json`: shared TypeScript settings.
- Create `sdk/example-chat-app/tsconfig.node.json`: backend and Vite config TypeScript settings.
- Create `sdk/example-chat-app/vite.config.ts`: Vite app config, backend dev proxy, test config.
- Create `sdk/example-chat-app/index.html`: Vite entry HTML.
- Create `sdk/example-chat-app/.env.example`: provider configuration template.
- Create `sdk/example-chat-app/README.md`: run instructions and feature demo guide.
- Create `sdk/example-chat-app/src/client/main.tsx`: React entrypoint.
- Create `sdk/example-chat-app/src/client/App.tsx`: app shell and `AgentChat` wiring.
- Create `sdk/example-chat-app/src/client/adapter.ts`: browser adapter implementation.
- Create `sdk/example-chat-app/src/client/styles.css`: compact app shell styles plus package CSS import surface.
- Create `sdk/example-chat-app/src/shared/types.ts`: HTTP DTOs shared by client and server.
- Create `sdk/example-chat-app/src/server/events.ts`: in-memory event store and SSE helpers.
- Create `sdk/example-chat-app/src/server/openai.ts`: chat-completions streaming client/parser.
- Create `sdk/example-chat-app/src/server/session-store.ts`: in-memory session, artifact, workspace state.
- Create `sdk/example-chat-app/src/server/demo-commands.ts`: scripted slash command event emitters.
- Create `sdk/example-chat-app/src/server/app.ts`: Express routes.
- Create `sdk/example-chat-app/src/server/index.ts`: server startup.
- Create `sdk/example-chat-app/tests/adapter.test.ts`: adapter REST/EventSource behavior.
- Create `sdk/example-chat-app/tests/events.test.ts`: event store cursor behavior.
- Create `sdk/example-chat-app/tests/openai.test.ts`: stream parser behavior.
- Create `sdk/example-chat-app/tests/demo-commands.test.ts`: scripted command event behavior.
- Create `sdk/example-chat-app/tests/workspace.test.ts`: workspace state behavior.

## Task 1: Package Scaffold

**Files:**
- Create: `sdk/example-chat-app/package.json`
- Create: `sdk/example-chat-app/tsconfig.json`
- Create: `sdk/example-chat-app/tsconfig.node.json`
- Create: `sdk/example-chat-app/vite.config.ts`
- Create: `sdk/example-chat-app/index.html`
- Create: `sdk/example-chat-app/.env.example`

- [ ] **Step 1: Create minimal package and config files**

Add a pnpm package named `@invergent/example-chat-app` with scripts:
`dev`, `dev:server`, `build`, `preview`, `test`, and `typecheck`.

- [ ] **Step 2: Verify package is visible to pnpm**

Run: `pnpm -C sdk/example-chat-app test -- --runInBand`

Expected: command reaches Vitest and reports no test files or fails only because tests are not added yet.

## Task 2: Server Event Store and State

**Files:**
- Create: `sdk/example-chat-app/src/shared/types.ts`
- Create: `sdk/example-chat-app/src/server/events.ts`
- Create: `sdk/example-chat-app/src/server/session-store.ts`
- Test: `sdk/example-chat-app/tests/events.test.ts`
- Test: `sdk/example-chat-app/tests/workspace.test.ts`

- [ ] **Step 1: Write failing event and workspace tests**

Cover monotonically increasing event ids, `after` replay, subscriber delivery,
seeded workspace tree, file reads, upload, and delete.

- [ ] **Step 2: Run tests to verify red**

Run: `pnpm -C sdk/example-chat-app test tests/events.test.ts tests/workspace.test.ts`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement event store and session store**

Implement focused TypeScript classes/functions for event append/replay,
subscription cleanup, session metadata, demo artifacts, and in-memory workspace
files.

- [ ] **Step 4: Run tests to verify green**

Run: `pnpm -C sdk/example-chat-app test tests/events.test.ts tests/workspace.test.ts`

Expected: PASS.

## Task 3: Chat-Completions Streaming Parser

**Files:**
- Create: `sdk/example-chat-app/src/server/openai.ts`
- Test: `sdk/example-chat-app/tests/openai.test.ts`

- [ ] **Step 1: Write failing parser tests**

Cover SSE `data:` lines, `[DONE]`, content deltas, usage-bearing chunks, invalid
JSON tolerance, provider errors, and configurable base URL/model.

- [ ] **Step 2: Run parser tests to verify red**

Run: `pnpm -C sdk/example-chat-app test tests/openai.test.ts`

Expected: FAIL because `openai.ts` does not exist.

- [ ] **Step 3: Implement parser and streaming request helper**

Implement `streamChatCompletions` using `fetch`, `TextDecoder`, and async
iteration over parsed chunks. Emit content deltas and final usage metadata
without depending on OpenAI SDK-specific APIs.

- [ ] **Step 4: Run parser tests to verify green**

Run: `pnpm -C sdk/example-chat-app test tests/openai.test.ts`

Expected: PASS.

## Task 4: Demo Commands

**Files:**
- Create: `sdk/example-chat-app/src/server/demo-commands.ts`
- Test: `sdk/example-chat-app/tests/demo-commands.test.ts`

- [ ] **Step 1: Write failing demo command tests**

Cover slash command listing and event emission for `/demo-tools`,
`/demo-artifacts`, `/demo-clarify`, `/demo-expert`, `/demo-errors`, and
`/demo-context`.

- [ ] **Step 2: Run command tests to verify red**

Run: `pnpm -C sdk/example-chat-app test tests/demo-commands.test.ts`

Expected: FAIL because `demo-commands.ts` does not exist.

- [ ] **Step 3: Implement demo command emitters**

Emit representative event sequences matching `AgentChatEventType`, and store
artifact payloads in the session store for artifact retrieval.

- [ ] **Step 4: Run command tests to verify green**

Run: `pnpm -C sdk/example-chat-app test tests/demo-commands.test.ts`

Expected: PASS.

## Task 5: Express Backend

**Files:**
- Create: `sdk/example-chat-app/src/server/app.ts`
- Create: `sdk/example-chat-app/src/server/index.ts`
- Modify: tests as needed for backend route coverage if route seams are clearer than direct module tests.

- [ ] **Step 1: Implement REST and SSE routes behind existing tested modules**

Routes:
`GET /api/config`, `GET /api/sessions`, `POST /api/sessions`,
`GET /api/sessions/:sessionId`, `DELETE /api/sessions/:sessionId`,
`POST /api/sessions/:sessionId/messages`, `POST /api/sessions/:sessionId/pause`,
`POST /api/sessions/:sessionId/retry`, `GET /api/sessions/:sessionId/events`,
`GET /api/sessions/:sessionId/artifacts/:artifactId`,
`POST /api/sessions/:sessionId/clarify/:toolCallId`,
`POST /api/sessions/:sessionId/expert-feedback`,
`GET /api/slash-commands`, `GET /api/sessions/:sessionId/workspace/tree`,
`GET /api/sessions/:sessionId/workspace/file`, `POST /api/sessions/:sessionId/workspace/upload`,
and `DELETE /api/sessions/:sessionId/workspace/file`.

- [ ] **Step 2: Run backend tests**

Run: `pnpm -C sdk/example-chat-app test tests/events.test.ts tests/openai.test.ts tests/demo-commands.test.ts tests/workspace.test.ts`

Expected: PASS.

## Task 6: Frontend Adapter and App Shell

**Files:**
- Create: `sdk/example-chat-app/src/client/adapter.ts`
- Create: `sdk/example-chat-app/src/client/App.tsx`
- Create: `sdk/example-chat-app/src/client/main.tsx`
- Create: `sdk/example-chat-app/src/client/styles.css`
- Test: `sdk/example-chat-app/tests/adapter.test.ts`

- [ ] **Step 1: Write failing adapter tests**

Cover URL construction, JSON body mapping, optional delete/expert methods, slash
command listing, workspace calls, and `EventSource` wrapping.

- [ ] **Step 2: Run adapter tests to verify red**

Run: `pnpm -C sdk/example-chat-app test tests/adapter.test.ts`

Expected: FAIL because the adapter module does not exist.

- [ ] **Step 3: Implement the adapter and app shell**

Render `AgentChat` with session selection controls. Keep shell UI small and
work-focused. Use the package adapter for all chat behavior.

- [ ] **Step 4: Run adapter tests to verify green**

Run: `pnpm -C sdk/example-chat-app test tests/adapter.test.ts`

Expected: PASS.

## Task 7: Documentation and Full Verification

**Files:**
- Create: `sdk/example-chat-app/README.md`
- Modify: `pnpm-lock.yaml`

- [ ] **Step 1: Add README and install dependencies**

Document setup, environment variables, real chat path, scripted slash commands,
and limitations.

- [ ] **Step 2: Run package verification**

Run:
`pnpm install`
`pnpm -C sdk/example-chat-app typecheck`
`pnpm -C sdk/example-chat-app test`
`pnpm -C sdk/example-chat-app build`

Expected: all commands exit 0.

- [ ] **Step 3: Start the dev server**

Run: `pnpm -C sdk/example-chat-app dev`

Expected: local server starts and reports a browser URL.
