# `/code` Connect UI (SDK + Web) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the "connect your coding plan" frontend — SDK adapter methods, composer `/code` slash entries gated by `codeAgentsEnabled`, a `CodingAgentsPanel` paste form, and the surogates/web wiring — against the already-shipped `/v1/coding-agents` routes.

**Architecture:** Three optional runtime-probed adapter methods mirror the Composio trio. The paste panel is an SDK component (like `IntegrationsPage`) consumed by a new `/coding-agents` web route. Composer commands thread a `codeAgentsEnabled` prop exactly like `deepResearchEnabled` (agent-chat → chat-thread → chat-composer).

**Tech Stack:** TypeScript, React 19, tsup build, vitest (happy-dom), pnpm workspaces; web consumes the SDK via `file:../sdk/agent-chat-react`.

**Spec:** `docs/superpowers/specs/2026-06-10-code-command-coding-agents-design.md` §8 (minus `CodeRunBlock`/events, which land with the execution plan).

**Conventions:** run `npm run typecheck` + `npx vitest run` in `sdk/agent-chat-react`; `npm run typecheck` in `web/`. No Plan/Task numbers or Co-Authored-By in commits.

## Progress

- [x] Task 1: Adapter types — 3 coding-agent methods
- [x] Task 2: Composer `/code` commands + `codeAgentsEnabled` threading
- [x] Task 3: `CodingAgentsPanel` paste form component
- [x] Task 4: web API wrappers + adapter implementation
- [x] Task 5: web `/coding-agents` route + AgentChat capability prop
- [x] Task 6: SDK build + version bump + web typecheck

---

### Task 1: Adapter types

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` (adapter interface, near the Composio block ~line 689)

Add (all optional, runtime-probed):

```typescript
export interface CodingAgentConnection {
  provider: "anthropic" | "openai";
  connected: boolean;
  auth_mode: "oauth" | "api_key" | null;
  expires_at: number | null;
}

// on AgentChatAdapter:
listCodingAgentConnections?(input: { agentId?: string }): Promise<{
  connections: CodingAgentConnection[];
}>;
submitCodingAgentCredential?(input: {
  agentId?: string;
  provider: string;
  mode: "oauth" | "api_key";
  value: string;
}): Promise<{ provider: string; connected: boolean; auth_mode: string }>;
disconnectCodingAgentProvider?(input: {
  agentId?: string;
  provider: string;
}): Promise<void>;
```

Export `CodingAgentConnection` from `index.ts`. Verify: `npm run typecheck` in the SDK.

### Task 2: Composer commands + capability prop

**Files:**
- Modify: `sdk/agent-chat-react/src/components/chat/chat-composer.tsx` (props ~147, builtinCommands ~357-388)
- Modify: `sdk/agent-chat-react/src/components/chat/chat-thread.tsx` (props ~132, pass-through ~2155)
- Modify: `sdk/agent-chat-react/src/agent-chat.tsx` (props ~41, pass-through ~259)
- Test: `sdk/agent-chat-react/src/components/chat/__tests__/` (mirror existing composer test if present, else add)

Add `codeAgentsEnabled?: boolean` (default false) threaded exactly like `deepResearchEnabled`. When true, append to `builtinCommands` (trailing-space convention for prompt-taking entries):

```typescript
if (codeAgentsEnabled) {
  base.push(
    { value: "/code claude ", label: "/code claude", description: "Run Claude Code on the workspace (your plan)" },
    { value: "/code codex ", label: "/code codex", description: "Run Codex on the workspace (your plan)" },
    { value: "/code status", label: "/code status", description: "Show connected coding agents" },
    { value: "/code login claude", label: "/code login claude", description: "Connect your Claude plan" },
    { value: "/code login codex", label: "/code login codex", description: "Connect your ChatGPT plan" },
  );
}
```

Memo deps: `[deepResearchEnabled, codeAgentsEnabled]`.

### Task 3: `CodingAgentsPanel`

**Files:**
- Create: `sdk/agent-chat-react/src/components/connections/coding-agents-panel.tsx`
- Modify: `sdk/agent-chat-react/src/index.ts` (export)
- Test: `sdk/agent-chat-react/src/components/connections/__tests__/coding-agents-panel.test.tsx`

Mirror `IntegrationsPage` structure. Per provider (claude/codex) one card: status line from `listCodingAgentConnections`, instructions text (`claude setup-token` / `codex login` + paste `~/.codex/auth.json`), mode toggle (subscription paste vs API key), masked `<textarea>`/password input, client-side format hint (claude oauth: must start `sk-ant-oat`; codex oauth: must parse as JSON), submit → `submitCodingAgentCredential`, disconnect → `disconnectCodingAgentProvider`, error surface from thrown adapter errors (422 detail). Refresh statuses after submit/disconnect. Render nothing if the adapter lacks the methods.

Tests (vitest + happy-dom): renders both providers from a fake adapter; submit calls adapter with trimmed value and refreshes status; disconnect calls adapter; format hint blocks obviously-wrong claude token client-side.

### Task 4: web API wrappers + adapter methods

**Files:**
- Create: `web/src/api/coding-agents.ts` (mirror `web/src/api/composio.ts`: `authFetch` against `/api/v1/coding-agents/...`)
- Modify: `web/src/features/chat/surogates-web-chat-adapter.ts` (3 methods next to the Composio block ~316)

Routes: `GET /api/v1/coding-agents/connections`, `POST /api/v1/coding-agents/{provider}/credential` (JSON `{mode, value}`), `DELETE /api/v1/coding-agents/{provider}`. Surface the FastAPI 422 `detail` string as the thrown Error message so the panel shows the validator's user-facing text.

### Task 5: web route + capability prop

**Files:**
- Create: `web/src/app/routes/coding-agents.tsx` (mirror `routes/integrations.tsx`; path `/coding-agents`, requireAuth, renders `CodingAgentsPanel` with `surogatesWebChatAdapter`)
- Modify: `web/src/app/routes/__root.tsx` or route registry (wherever `integrations` route is registered)
- Modify: `web/src/features/chat/chat-page.tsx` (~199: pass `codeAgentsEnabled` to `<AgentChat>`)

### Task 6: build + version + typecheck

- Bump `sdk/agent-chat-react/package.json` to 1.8.0 (ops Studio consumes from npm; publish happens via the release workflow, out of scope here).
- `npm run build` + `npx vitest run` in the SDK; `npm run typecheck` in `web/`.
- Commit.

## Out of scope

- `CodeRunBlock`, `code.run_*` SSE event registration (execution plan).
- npm publish + `surogate-ops/frontend` integration (needs the published package; `agent-chat-react.d.ts` update happens at upgrade time).
