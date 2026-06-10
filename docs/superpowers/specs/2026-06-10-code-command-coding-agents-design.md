# `/code` — Orchestrating External Coding Agents in Surogates

**Status:** Design approved (pending written-spec review)
**Date:** 2026-06-10
**Scope:** Add a `/code` chat command to the Surogates harness that runs external
coding-agent CLIs (Claude Code, Codex) on behalf of the end user, authenticated
with the user's own subscription plan via OAuth (API key as fallback).

---

## 1. Goal & Motivation

Let a Surogates chat user hand a coding task to a best-in-class external coding
agent and get the result streamed back into the same chat, e.g.:

```
/code claude "fix issue 35"
/code codex "summarize this repo"
```

The hard requirement is **subscription auth**: the user authenticates to Anthropic
and OpenAI through the chat UI so the coding agents draw on the user's existing
coding plans (Claude Pro/Max, ChatGPT Plus/Pro), rather than burning platform API
credits. Authentication is handled inside the chat experience powered by the
`@invergent/agent-chat-react` SDK (`/work/surogates/sdk/agent-chat-react`).

v1 targets **Claude Code and Codex only**.

## 2. The Controlling Constraint: ToS-Compliant Subscription Auth

Research into the mid-2026 state of both providers established the single most
important constraint on this design:

- **Anthropic (Feb 2026 ToS):** Subscription OAuth tokens may be used **only**
  inside Claude Code, Claude.ai, and Claude Desktop. Using them in a third-party
  API client violates the Consumer ToS, and Anthropic enforces this server-side
  (rejecting subscription tokens from non-Claude-Code clients). **However**,
  running the *actual* `claude` binary headlessly with a subscription token
  (`CLAUDE_CODE_OAUTH_TOKEN`) is the sanctioned path.
- **OpenAI/Codex:** Permissive. `auth.json` portability and CI reuse of
  ChatGPT-plan credentials are officially documented; a real device-code flow
  (`codex login --device-auth`) exists.

**Therefore the only compliant architecture is to orchestrate the real vendor CLI
binaries inside the sandbox.** We never extract the user's tokens to call the
provider APIs ourselves. This is the same model as the `headless-cli` project
studied for this design, and we borrow its command/output shapes (a slim Python
port) without adopting the whole CLI.

> **Risk flag (product/legal):** Anthropic has signalled that CI/Agent-SDK use of
> subscription plans moves to a separate "Agent SDK credit" pool starting
> 2026-06-15. Running the `claude` binary interactively-headless on the user's own
> plan is the documented path, but this evolving policy needs product/legal
> sign-off before launch. See §11.

## 3. Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Working directory for a run | The session's existing S3 `/workspace` only. No git-clone / `--repo` in v1. |
| OAuth capture UX | Popup-launched, **backend holds & refreshes tokens**. Completion differs per provider (see §5). |
| Run/interaction model | Streamed one-shot; conversation resume via `--session <alias>`. No interactive mid-run Q&A in v1. |
| API-key fallback | Yes — subscription OAuth primary, bring-your-own API key as an alternative. |
| Session-resume storage | **Within the same chat thread only**, via event-log lookup. No new table. |
| Wiring | Builtin `/code` slash command (pre-LLM), not an LLM-driven tool. |
| CLI integration strategy | Run the real binaries; port headless-cli command/output logic to Python. |

## 4. Architecture Overview

Two layers, built in order: **auth foundation** first, then **execution**.

```
 Chat UI (agent-chat-react)                      Surogates backend
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ composer: /code … entries │  POST /messages   │ api/routes/sessions.py       │
 │ CodingAgentsPanel (popup) │ ───────────────►  │   → user.message event       │
 │ CodeRunBlock (progress)   │                   │   → enqueue_session (Redis)  │
 └──────────┬────────────────┘                   └──────────────┬───────────────┘
            │ connect routes (REST)                             │ wake()
            ▼                                                   ▼
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ api/routes/coding_agents  │                   │ harness/loop.py dispatch     │
 │  authorize / complete /   │   CredentialVault │   "/code …" → CodeCommandMixin│
 │  connections / api-key /  │ ◄───────────────► │   ._handle_code_command      │
 │  disconnect               │   (Fernet, vault) │            │                 │
 └──────────┬────────────────┘                   └───────────┬──────────────────┘
            │ surogates/coding_agents/oauth.py               │ surogates/coding_agents/
            ▼ (PKCE / device / refresh)                      ▼ runner.py + agents.py
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ Anthropic / OpenAI OAuth  │                   │ per-session K8s sandbox pod  │
 │ token exchange + refresh  │                   │  real `claude` / `codex` CLI │
 └───────────────────────────┘                   │  (transient cred injected)   │
                                                  └──────────────────────────────┘
```

New modules under a new package `surogates/coding_agents/`:

| File | Responsibility |
|---|---|
| `oauth.py` | `AnthropicOAuthProvider`, `OpenAIOAuthProvider`: `authorize_url()`, `exchange()`, `refresh()`, `ensure_fresh(bundle)`. PKCE + device-code + refresh. |
| `agents.py` | Slim port of headless-cli `agents.ts` + `output.ts` for `claude` and `codex` only: command builders and stream-json output parsing (final message, native session id, usage). |
| `runner.py` | Run orchestrator: refresh creds → inject into pod → spawn CLI → poll log → emit progress → capture result → clean up. |
| `credentials.py` | Vault convention helpers: `store_bundle()`, `load_bundle()`, `bundle_status()` over `CredentialVault`. |

Existing modules touched:

| File | Change |
|---|---|
| `harness/slash_skill.py` | Add `"code"` to `_BUILTIN_SLASH_COMMANDS`. |
| `harness/loop.py` | Dispatch `"/code"` to the new mixin in `wake()`. |
| `harness/loop_code_commands.py` (new) | `CodeCommandMixin` mixed into `AgentHarness`. |
| `session/events.py` | New `EventType` members. |
| `api/app.py` | Mount the new router. |
| `sdk/agent-chat-react/*` | Adapter methods, composer entries, connect UI, run renderer, event types. |
| `images/sandbox/Dockerfile` | Install `@anthropic-ai/claude-code` + `@openai/codex`. |
| k8s NetworkPolicy | Allow pod egress to `api.anthropic.com` + `api.openai.com`. |

## 5. Auth Foundation

### 5.1 Credential model (no schema change)

Stored via the existing `CredentialVault` (Fernet-encrypted `credentials` table),
**user-scoped**: `org_id` = session org, `user_id` = end user. One row per
provider, named `code_oauth:anthropic` / `code_oauth:openai`. The encrypted value
is a JSON bundle:

```json
{
  "version": 1,
  "provider": "anthropic",
  "auth_mode": "oauth",
  "access_token": "sk-ant-oat01-...",
  "refresh_token": "sk-ant-ort01-...",
  "expires_at": 1750000000000,
  "account_id": null,
  "subscription_type": "max",
  "scopes": ["user:inference", "user:profile"],
  "api_key": null,
  "created_at": "...",
  "updated_at": "..."
}
```

`auth_mode: "api_key"` populates `api_key` and nulls the OAuth fields.

**No org fallback on resolution.** Resolution always passes the explicit
`user_id`; a missing per-user bundle means "not connected" and never silently
falls back to an org-scoped credential (which would bill another principal's
plan). This deliberately diverges from the `session_llm` / MCP-proxy user→org
fallback.

The bundle is opaque to the vault — no migration is needed. Refresh tokens live
only in the bundle (worker-side); they never enter a sandbox pod (§6.2).

### 5.2 Connect API routes — `surogates/api/routes/coding_agents.py`

Tenant-authed, copying the Composio end-user pattern
(`api/routes/composio.py`: `get_current_tenant` + `agent_runtime_context_dep` +
`_require_end_user` to reject service accounts and cross-tenant access). Unlike
Composio (which delegates token custody to its cloud), **we** exchange and store
the tokens.

| Route | Behavior |
|---|---|
| `GET /v1/coding-agents/connections` | `[{provider, connected, auth_mode, subscription_type?, expires_at?}]`. Status only — **never returns plaintext**. SDK polls this. |
| `POST /v1/coding-agents/{provider}/authorize` | Start a flow → `{flow, popupUrl, userCode?, correlationId}`. PKCE verifier / device_code stored transiently in Redis keyed by `correlationId` (short TTL). |
| `POST /v1/coding-agents/{provider}/complete` | Anthropic: `{correlationId, code}` → exchange code+verifier → store bundle. (OpenAI completes via the backend device-poll; this route is a no-op/confirm for OpenAI.) |
| `POST /v1/coding-agents/{provider}/api-key` | `{apiKey}` → store an `api_key`-mode bundle. |
| `DELETE /v1/coding-agents/{provider}` | Delete the user-scoped bundle (logout). |

### 5.3 Per-provider OAuth mechanics

The vendor OAuth public clients do **not** permit registering an arbitrary
backend callback (Anthropic's client allows only loopback or its own
code-display page; OpenAI's Codex client mandates `localhost:1455`). So a silent
Composio-style redirect is impossible for *subscription* auth. The UX still
matches the chosen "popup + backend-held tokens" shape:

- **Anthropic — popup + paste-code.** Backend generates a PKCE verifier/challenge
  and an authorize URL (`redirect_uri` = the vendor code-display page). The popup
  opens the consent page; the provider shows a one-time code; the user pastes it
  into a chat field; the backend exchanges `code + verifier` for tokens and stores
  the bundle. Access token ≈ 8 h; backend refreshes server-side using the refresh
  token before each run.
- **OpenAI — device-code + poll.** Backend calls the device-authorization
  endpoint, returns `verification_uri_complete` + `userCode`; the popup opens it
  (code pre-filled); the backend runs a short-lived poll task against the token
  endpoint until authorized, then stores the bundle. The SDK watches
  `GET /connections` to flip to "connected." (Requires the user to have enabled
  device auth in ChatGPT settings — documented prerequisite; API-key fallback
  otherwise.)

`oauth.py` keeps **client IDs, authorize/token/device endpoints, and scopes
config-driven** (e.g. surogates config keys), not hardcoded, because the
published values are reverse-engineered/community-sourced and may change.

## 6. Execution Layer

### 6.1 Command builders + output parsing — `agents.py`

A focused Python port of the relevant headless-cli logic, for two agents only.
Non-interactive `stream-json` modes (no PTY — sidesteps the missing-`ptyprocess`
sandbox gotcha):

- **claude:** `claude -p <prompt-on-stdin> --output-format stream-json --verbose`
  `[--dangerously-skip-permissions | read-only mode]` `[--model <m>]`
  `[--effort <e>]` `[--session-id <uuid> | --resume <id>]`.
- **codex:** `codex exec --json --dangerously-bypass-approvals-and-sandbox
  --skip-git-repo-check` `[--model <m>]` `[reasoning-effort]` `<prompt>`.

Normalized options: `--model`, `--effort low|medium|high|xhigh`,
`--allow read-only` (default is the agent's bypass/yolo mode), `--session <alias>`.
Output parsing mines the JSONL stream (tolerant per-line JSON) for: the final
assistant message, the native session id (for resume), and token usage.

### 6.2 Credential injection — transient, access-token-only, off-S3

The worker handler (which has vault access) orchestrates each run:

1. Load the user's bundle; `ensure_fresh()` refreshes the access token
   server-side if near expiry. **Refresh tokens never leave the worker.**
2. Write a fresh credential into a **pod-local** directory (e.g. `/run/code/...`,
   on the pod's writable layer, **not** the s3fs `/workspace` mount), pointed at
   via `CLAUDE_CONFIG_DIR` / `CODEX_HOME`:
   - claude: `CLAUDE_CODE_OAUTH_TOKEN` (or a `.credentials.json` holding the
     access token); API-key mode sets `ANTHROPIC_API_KEY`.
   - codex: an `auth.json` containing the access token (+`account_id`); API-key
     mode sets `OPENAI_API_KEY`.
3. `HOME` stays `/workspace` so the agent edits workspace files; only the
   config/credential dir is redirected off S3. This keeps tokens off the S3
   bucket and out of the model's reach (the credential dir is not a path the
   model's shell tools read; the terminal denylist already blocks
   `.credentials.json`-style reads).
4. Run the CLI (§6.3), then **delete the credential file**.

This deliberately—but minimally—breaks the platform's "secrets never enter the
sandbox" invariant: only a short-lived **access** token is exposed, for the
duration of one run, in a pod-local dir. Flagged for security review (§11).

### 6.3 Beating the sandbox ceilings

The K8s sandbox imposes a ~305 s foreground-exec ceiling and a 1 h pod deadline,
and the cross-pod background-process manager is unreliable. The runner therefore
self-supervises:

1. One exec spawns the CLI detached: `nohup <cmd> > /run/code/run-<id>.log 2>&1 &`
   (writes its PID to a file).
2. A poll loop issues **short** (<305 s) exec reads that tail the new bytes of the
   log, parses freshly-emitted `stream-json` lines, emits **coalesced** progress
   events (never per-line — avoids event-log bloat), renews the session lease
   (interval < the 60 s lease TTL), and checks for interrupt.
3. On process exit, parse the final message + native session id + usage from the
   captured log.
4. Clean up the credential file (and optionally retain the log as a session
   artifact).

**Hard limit:** a single run is capped by the 1 h pod deadline. v1 documents this
and emits a clean error if the pod is reclaimed mid-run.

## 7. Harness `/code` Command

### 7.1 Dispatch

- Add `"code"` to `_BUILTIN_SLASH_COMMANDS` (`harness/slash_skill.py`) so it never
  resolves as a tenant skill.
- In `loop.py` `wake()`, alongside the other builtins, match against the **raw**
  event text (`_latest_user_event_text(all_events)`, **not** the rebuilt message —
  attachment notes can push the leading `/` off the start):

  ```python
  if last_user_content == "/code" or last_user_content.startswith("/code "):
      await self._handle_code_command(session, last_user_content, lease)
      return
  ```

- `CodeCommandMixin` lives in `harness/loop_code_commands.py` and is mixed into
  `AgentHarness`, following `OutcomeCommandMixin`
  (`harness/loop_outcome_commands.py`) for subcommand parsing, event emission, and
  store access.

### 7.2 Subcommands

| Subcommand | Behavior |
|---|---|
| `/code` / `/code help` | Usage text (emit an assistant-visible `LLM_RESPONSE`). |
| `/code status` | Connection status per provider + recent runs in this thread. |
| `/code login <provider>` | Kick the connect UI (emit an event the SDK renders as a Connect card / opens the panel). |
| `/code logout <provider>` | Delete the user-scoped bundle. |
| `/code claude\|codex <prompt> [flags]` | Run. Flags: `--model`, `--effort`, `--allow read-only`, `--session <alias>`. If not connected → emit a "connect first" message with the connect affordance. |

Builtin handling early-returns (no main LLM call → deterministic, no token cost).
A handler that returns must always emit at least one assistant-visible event, or
the UI shows nothing for the turn.

### 7.3 Lifecycle correctness

- **Idempotency:** before launching, scan events for an existing
  `CODE_RUN_STARTED` with the same raw message and skip relaunch (crash-recovery
  re-wakes re-process the same user message; mirrors
  `_slash_loop_already_processed`).
- **Lease:** the run can take minutes; renew the lease throughout (mirrors
  `ask_user_question`) so the orphan sweeper doesn't reclaim the session.
- **Interrupt:** on session pause, the poll loop detects the interrupt and kills
  the pod-side CLI process.

### 7.4 Session resume (within the chat thread)

`CODE_RUN_RESULT` events carry `{alias, agent, native_session_id}`. `--session
<alias>` resolves by scanning the **current session's** event log backwards for
the latest `CODE_RUN_RESULT` with that alias and agent, and passes the native id
to the CLI's resume flag. No new table; resume is scoped to the same chat thread
(a fresh chat starts clean). A cross-thread store is a deferred follow-up.

### 7.5 New event types (`session/events.py`)

`CODE_RUN_STARTED`, `CODE_RUN_PROGRESS`, `CODE_RUN_RESULT`, and an auth-prompt
event (or reuse the `ask_user_question` round-trip / an inbox action). Token-shaped
data is redacted by `emit_event`; **OAuth tokens must never be placed in event
payloads** (events stream verbatim to the browser over SSE).

## 8. SDK / Frontend (`agent-chat-react` + consumers)

- **Adapter methods** (all optional, runtime-probed, added to every consumer —
  `surogates/web`, `surogate-ops/frontend`, the example app, website-widget):
  `listCodingAgentConnections`, `authorizeCodingAgentProvider`,
  `completeCodingAgentAuth`, `disconnectCodingAgentProvider`,
  `setCodingAgentApiKey`.
- **Composer:** add `/code claude `, `/code codex `, `/code login claude `,
  `/code login codex `, `/code status ` to `builtinCommands`
  (`chat-composer.tsx`), trailing-space convention so the cursor lands on the
  args. **Gated behind a new `codeAgentsEnabled` capability prop**, threaded
  exactly like `deepResearchEnabled` (read from the agent record by hosts).
- **Connect UI:** a `CodingAgentsPanel` mirroring
  `components/connections/integrations-page.tsx` — `openOAuthPopup` (per-provider
  window name to avoid the shared `"composio-oauth"` name), a paste-code input for
  Anthropic, device-code display + `GET /connections` polling (2 s / 180 s) for
  OpenAI.
- **Run renderer:** a `CodeRunBlock` mirroring `TerminalToolBlock` /
  `ProcessToolBlock` (ansi-aware) for streamed progress + final result.
- **Events:** any new SSE event type must be added in **three** places or it is
  silently dropped — the `AgentChatEventType` union, `AGENT_CHAT_LISTENED_EVENTS`
  (`runtime/events.ts`), and the `applyAgentChatEvent` switch (`runtime/reducer.ts`).
- **Publish:** ops consumes `@invergent/agent-chat-react` from the npm registry, so
  SDK changes require a version bump + publish before Studio sees them; update
  `surogate-ops/frontend/src/types/agent-chat-react.d.ts` if types change.
- **EventSource auth:** any new streaming endpoint must accept a query-string
  token (EventSource cannot send `Authorization` headers).

## 9. Infrastructure

- **Sandbox image** (`images/sandbox/Dockerfile`): `npm install -g
  @anthropic-ai/claude-code @openai/codex` in the existing global-npm layer
  (Node 20 is already present; `PATH` already includes the global bin dir). No
  per-tenant image override exists today, so this ships the CLIs for everyone.
- **NetworkPolicy:** allow sandbox-pod egress to `api.anthropic.com` and
  `api.openai.com` (and `chatgpt.com` if Codex requires it). OAuth token exchange
  runs in the worker (already has egress); only the CLI's provider API calls need
  pod egress. Verify whether a restricting NetworkPolicy actually exists in the
  cluster.
- **Encryption:** `SUROGATES_ENCRYPTION_KEY` is already shared between the ops
  server and worker pods; the vault works as-is.
- **Cred dir:** a pod-local credential directory convention
  (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`) off the S3 mount.

## 10. Non-Goals (v1)

- Git-repo clone / `--repo` / `--work-dir` (runs operate on the session
  `/workspace` only).
- Agents other than `claude` and `codex` (no cursor/gemini/opencode/pi).
- Interactive mid-run Q&A (the agent pausing to ask the user a question).
- Detached/background jobs with status polling.
- Runs longer than the ~1 h pod deadline.
- Cross-chat-thread session resume (event-log lookup is thread-scoped).
- Studio org-level visibility/management of `/code` tokens.
- headless-cli's roles/teams/run-graph orchestration and cron scheduling.

## 11. Risks & Open Questions

1. **Anthropic ToS / June-15 policy.** Compliant only by running the real `claude`
   binary on the user's plan. The 2026-06-15 "Agent SDK credit pool" change needs
   product/legal sign-off. **Verify** that a PKCE access token works as
   `CLAUDE_CODE_OAUTH_TOKEN`; if not, prefer driving `claude setup-token`
   (1-year token) as the capture mechanism.
2. **Unofficial OAuth params.** Client IDs / endpoints / scopes for both providers
   are community-sourced. Keep them config-driven and verify against live behavior
   during implementation; expect possible breakage if vendors rotate them.
3. **OpenAI device-auth prerequisite.** Device auth must be enabled in the user's
   ChatGPT settings. Document it; fall back to API key when unavailable.
4. **Secrets-in-pod.** Injecting an access token into the sandbox breaks the
   "secrets never enter the sandbox" invariant. Mitigated to transient,
   access-token-only, pod-local, deleted after the run — but requires a security
   review and a redaction check (CLIs echoing env/config).
5. **s3fs performance.** Git-heavy coding agents on the FUSE-mounted `/workspace`
   may be slow; workspace-only v1 keeps repos small. Watch for FUSE quirks already
   documented in the sandbox.
6. **Pod deadline.** Long runs (>~1 h) are killed. Acceptable for v1; chunking is a
   future enhancement.

## 12. Implementation Phasing

1. **Auth foundation:** `oauth.py`, vault convention (`credentials.py`), connect
   routes, SDK adapter methods + `CodingAgentsPanel` + popup, `/code login/status/
   logout` subcommands. Ship "connect your plan" end-to-end before any execution.
2. **Execution:** `agents.py` (command builders + output parsing), `runner.py`
   (inject/spawn/poll/capture), `/code claude|codex` subcommand, new event types +
   `CodeRunBlock`, sandbox image + NetworkPolicy. Ship a streamed one-shot run.
3. **Resume & polish:** `--session` event-log resume, `--model`/`--effort`/
   `--allow` normalization, usage reporting, idempotency/interrupt hardening,
   docs (`docs/commands/index.md`, usage docs).
