# `/code` — Orchestrating External Coding Agents in Surogates

**Status:** Design approved (revised after spec review)
**Date:** 2026-06-10
**Scope:** Add a `/code` chat command to the Surogates harness that runs external
coding-agent CLIs (Claude Code, Codex) on behalf of the end user, authenticated
with the user's own subscription plan (API key as fallback).

> **Revision note (post-review):** the credential **capture** mechanism changed
> from a backend-driven OAuth/PKCE flow to **pasting a binary-minted credential**
> (capture "A"), because driving PKCE ourselves is the third-party pattern
> Anthropic bans. Conversation **resume** was dropped from v1. The prompt-injection
> detector is **exempted** for `/code` argument text. See §13 for the review trail.

---

## 1. Goal & Motivation

Let a Surogates chat user hand a coding task to a best-in-class external coding
agent and get the result streamed back into the same chat, e.g.:

```
/code claude "fix issue 35"
/code codex "summarize this repo"
```

The hard requirement is **subscription auth**: the user authenticates to Anthropic
and OpenAI so the coding agents draw on the user's existing coding plans (Claude
Pro/Max, ChatGPT Plus/Pro), rather than burning platform API credits.
Authentication is handled inside the chat experience powered by the
`@invergent/agent-chat-react` SDK (`/work/surogates/sdk/agent-chat-react`).

v1 targets **Claude Code and Codex only**.

## 2. The Controlling Constraint: ToS-Compliant Subscription Auth

Research into the mid-2026 state of both providers established the single most
important constraint on this design:

- **Anthropic (Feb 2026 ToS):** Subscription OAuth tokens may be used **only**
  inside Claude Code, Claude.ai, and Claude Desktop. Using them in a third-party
  API client violates the Consumer ToS, and Anthropic enforces this server-side.
  **The sanctioned headless path is to run the *actual* `claude` binary** with a
  token the binary itself minted via `claude setup-token` (a ~1-year token
  consumed as `CLAUDE_CODE_OAUTH_TOKEN`).
- **OpenAI/Codex:** Permissive. `auth.json` portability and CI reuse of
  ChatGPT-plan credentials are officially documented; the `codex` binary writes
  and refreshes `~/.codex/auth.json` itself.

**Therefore the only compliant architecture is to orchestrate the real vendor CLI
binaries inside the sandbox, using credentials the vendor binaries minted.** We
never run an OAuth flow ourselves and never call the provider APIs directly. This
is the same model as the `headless-cli` project studied for this design, and we
borrow its command/output shapes (a slim Python port) without adopting the whole
CLI.

> **Risk flag (product/legal):** Anthropic has signalled that CI/Agent-SDK use of
> subscription plans moves to a separate "Agent SDK credit" pool starting
> 2026-06-15. Running the `claude` binary on the user's own plan with a
> `setup-token` is the documented path, but this evolving policy needs
> product/legal sign-off before launch. See §11.

## 3. Decisions (from brainstorming + review)

| Question | Decision |
|---|---|
| Working directory for a run | The session's existing S3 `/workspace` only. No git-clone / `--repo` in v1. |
| **Credential capture** | **The user runs the vendor CLI's own login locally and pastes the binary-minted credential** (`claude setup-token` output / `codex` `auth.json`). We store it; we never run an OAuth flow. API key may be pasted as a fallback. |
| Run/interaction model | Streamed one-shot. No interactive mid-run Q&A in v1. |
| **Conversation resume** | **Not in v1.** Each `/code` run is independent. |
| API-key fallback | Yes — subscription credential primary, bring-your-own API key as an alternative. |
| **Prompt-injection screen** | **`/code` argument text is exempted** from the `send_message` detector (it is a command to the user's own coding agent, never fed to the platform LLM). |
| Wiring | Builtin `/code` slash command (pre-LLM), not an LLM-driven tool. |
| CLI integration strategy | Run the real binaries; port headless-cli command/output logic to Python. |

## 4. Architecture Overview

Two layers, built in order: **auth foundation** first, then **execution**. Capture
"A" makes the auth foundation small — there is no OAuth implementation, only
credential intake, validation, and storage.

```
 Chat UI (agent-chat-react)                      Surogates backend
 ┌───────────────────────────┐                   ┌──────────────────────────────┐
 │ composer: /code … entries │  POST /messages   │ api/routes/sessions.py       │
 │ CodingAgentsPanel (paste) │ ───────────────►  │   (injection screen EXEMPTS  │
 │ CodeRunBlock (progress)   │                   │    /code) → user.message     │
 └──────────┬────────────────┘                   │    → enqueue_session         │
            │ credential route (REST, TLS)        └──────────────┬───────────────┘
            ▼  (paste token / auth.json / api key)               │ wake()
 ┌───────────────────────────┐                                   ▼
 │ api/routes/coding_agents  │   CredentialVault │ ┌──────────────────────────────┐
 │  submit / status / delete │ ◄───────────────► │ │ harness/loop.py dispatch     │
 │  (validate + store bundle)│   (Fernet, vault) │ │   "/code …" → CodeCommandMixin│
 └───────────────────────────┘                     │   ._handle_code_command      │
                                                    │            │ runner.py        │
                                                    ▼            ▼ + agents.py
                                          ┌──────────────────────────────┐
                                          │ per-session K8s sandbox pod  │
                                          │  real `claude` / `codex` CLI │
                                          │  (transient cred injected)   │
                                          └──────────────────────────────┘
```

New modules under a new package `surogates/coding_agents/`:

| File | Responsibility |
|---|---|
| `credentials.py` | Vault convention over `CredentialVault`: `store_bundle()`, `load_bundle()`, `bundle_status()`, plus `validate_pasted()` for each provider/mode (sanity-check a pasted `setup-token`, `auth.json`, or API key before storing). No OAuth flow. |
| `agents.py` | Slim port of headless-cli `agents.ts` + `output.ts` for `claude` and `codex` only: command builders and stream-json output parsing (final message, usage). No resume. |
| `runner.py` | Run orchestrator: load+inject credential into the pod → spawn CLI → poll log → emit progress → capture result → clean up (+ codex `auth.json` write-back, §6.2). |

There is **no `oauth.py`** — capture A removes it.

Existing modules touched:

| File | Change |
|---|---|
| `api/routes/sessions.py` | Exempt `/code …` body from the prompt-injection screen (§7.0). |
| `harness/slash_skill.py` | Add `"code"` to `_BUILTIN_SLASH_COMMANDS`. |
| `harness/loop.py` | Dispatch `"/code"` to the new mixin in `wake()`. |
| `harness/loop_code_commands.py` (new) | `CodeCommandMixin` mixed into `AgentHarness`. |
| `session/events.py` | New `EventType` members. |
| `api/app.py` | Mount the new router. |
| `sdk/agent-chat-react/*` | Adapter methods, composer entries, paste-to-connect UI, run renderer, event types. |
| `images/sandbox/Dockerfile` | Install `@anthropic-ai/claude-code` + `@openai/codex`. |
| `images/worker/srt-settings.json`, `harness/context_files.py`, `scheduled/prompt_guard.py` | Extend secret-read deny patterns to cover `/run/code`, `auth.json`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR`. |
| k8s NetworkPolicy | Allow sandbox-pod egress to `api.anthropic.com` + `api.openai.com`. |

## 5. Auth Foundation (capture A)

### 5.1 How the user connects

The user runs the vendor CLI's own login **on their own machine**, then pastes the
resulting binary-minted credential into the chat. We validate and store it; the
secret travels over the dedicated TLS credential route, **never** through the chat
message body or the event log.

| Provider | What the user runs | What they paste | Stored as |
|---|---|---|---|
| **Claude (sub)** | `claude setup-token` | the printed ~1-year token (`sk-ant-oat01-…`) | `auth_mode: "oauth"`, `oauth_token`, `token_kind: "setup_token"` |
| **Claude (API)** | — | `ANTHROPIC_API_KEY` (`sk-ant-api03-…`) | `auth_mode: "api_key"`, `api_key` |
| **Codex (sub)** | `codex login` (or `--device-auth`) | the contents of `~/.codex/auth.json` | `auth_mode: "oauth"`, `auth_json` |
| **Codex (API)** | — | `OPENAI_API_KEY` | `auth_mode: "api_key"`, `api_key` |

`validate_pasted()` rejects malformed input early with a helpful message (token
prefix checks; `auth.json` must parse and contain `tokens.access_token`).

### 5.2 Credential model (no schema change)

Stored via the existing `CredentialVault` (Fernet-encrypted `credentials` table),
**user-scoped**: `org_id` = session org, `user_id` = end user. One row per
provider, named `code_cred:anthropic` / `code_cred:openai`. The encrypted value is
a JSON bundle, e.g. Claude subscription:

```json
{
  "version": 1,
  "provider": "anthropic",
  "auth_mode": "oauth",
  "token_kind": "setup_token",
  "oauth_token": "sk-ant-oat01-...",
  "api_key": null,
  "auth_json": null,
  "expires_at": 1781568000000,
  "created_at": "...",
  "updated_at": "..."
}
```

`expires_at` is informational (for "reconnect by" hints); we do not refresh
Claude tokens — the `setup-token` is valid ~1 year. The bundle is opaque to the
vault, so **no migration is needed**.

**No org fallback on resolution.** Resolution always passes the explicit
`user_id`; a missing per-user bundle means "not connected" and never silently
falls back to an org-scoped credential (which would bill another principal's
plan). This deliberately diverges from the `session_llm` / MCP-proxy user→org
fallback.

### 5.3 Credential routes — `surogates/api/routes/coding_agents.py`

Tenant-authed, end-user only, copying the Composio pattern
(`api/routes/composio.py`: `get_current_tenant` + `agent_runtime_context_dep` +
`_require_end_user`). Because capture A has no OAuth handshake, the surface is just
three routes:

| Route | Behavior |
|---|---|
| `GET /v1/coding-agents/connections` | `[{provider, connected, auth_mode, expires_at?}]`. Status only — **never returns plaintext**. SDK polls / refreshes this. |
| `POST /v1/coding-agents/{provider}/credential` | Body `{mode, value}` → `validate_pasted()` → store bundle (upsert). The `value` is the pasted token / `auth.json` / API key. |
| `DELETE /v1/coding-agents/{provider}` | Delete the user-scoped bundle (disconnect). |

The submitted secret is never echoed back, never written to the event log, and
never returned by any GET.

## 6. Execution Layer

### 6.1 Command builders + output parsing — `agents.py`

A focused Python port of the relevant headless-cli logic, for two agents only.
Non-interactive `stream-json` modes (no PTY — sidesteps the missing-`ptyprocess`
sandbox gotcha):

- **claude:** `claude -p <prompt-on-stdin> --output-format stream-json --verbose`
  `[--dangerously-skip-permissions | read-only mode]` `[--model <m>]` `[--effort <e>]`.
- **codex:** `codex exec --json --dangerously-bypass-approvals-and-sandbox
  --skip-git-repo-check` `[--model <m>]` `[reasoning-effort]` `<prompt>`.

Normalized options: `--model`, `--effort low|medium|high|xhigh`,
`--allow read-only` (default is the agent's bypass/yolo mode). **No `--session`
flag in v1.** Output parsing mines the JSONL stream (tolerant per-line JSON) for
the final assistant message and token usage.

### 6.2 Credential injection — transient, off-S3, env-hygienic

The worker handler (which has vault access) orchestrates each run:

1. Load the user's bundle. (No refresh step for Claude — the `setup-token` is
   long-lived.)
2. Place the credential in a **pod-local** directory (e.g. `/run/code/...`, on the
   pod writable layer, **not** the s3fs `/workspace` mount):
   - **claude OAuth:** export `CLAUDE_CODE_OAUTH_TOKEN=<token>` from a pod-local
     launcher script or equivalent run-local env injection (pod-level
     `SandboxSpec.env` is stripped by the terminal allowlist, so the runner must
     inject credentials at process-launch time). API-key mode:
     `ANTHROPIC_API_KEY` through the same launch path. **Do not put secrets in
     logged command strings or shell history.**
   - **codex OAuth:** write the pasted `auth.json` to `CODEX_HOME/auth.json`
     (pod-local), set `CODEX_HOME` through the same launch path. API-key mode:
     `OPENAI_API_KEY` through the launch path.
3. **Env hygiene:** before spawning, scrub conflicting provider vars from the
   child env. `claude`'s precedence is `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN`,
   so a stray `ANTHROPIC_API_KEY` in the pod would silently override the user's
   subscription token. (headless-cli strips it for exactly this reason.)
4. `HOME` stays `/workspace` so the agent edits workspace files; only the
   credential dir is redirected off S3 via `CLAUDE_CONFIG_DIR` / `CODEX_HOME`.
5. Run the CLI (§6.3).
6. **Codex write-back:** after the run, read `CODEX_HOME/auth.json` back and
   re-store the bundle if it changed — `codex` refreshes its own token in-pod, and
   write-back keeps the vault copy fresh so the user isn't forced to re-paste every
   ~8 days. Then **delete the pod-local credential dir.**

This deliberately—but minimally—breaks the platform's "secrets never enter the
sandbox" invariant. Two consequences are called out for security review (§11):
the Claude token is **long-lived** (a 1-year `setup-token` at rest in the pod for
the run's duration is higher-value than a short access token), and the codex
`auth.json` carries a refresh token (sanctioned by OpenAI's documented `auth.json`
portability). Mitigations: pod-local dir off S3, process-launch env/file only
(never `SandboxSpec.env`), no plaintext secrets in command logs, deletion after
each run, and explicit deny patterns for `/run/code`, `auth.json`, `CODEX_HOME`,
and `CLAUDE_CONFIG_DIR` in the terminal/runtime policy plus prompt/context guards.

Before launch, run an empirical isolation preflight for both vendor CLIs: ask the
agent to print its environment, inspect `CODEX_HOME`, read `/run/code`, and spawn
a shell subprocess. If either CLI exposes subscription credentials to its own tool
subprocesses, subscription mode is not shippable as v1 without a stronger
isolation boundary (for example a brokered credential helper or a provider-backed
safe mode). The deny patterns reduce accidental leakage through Surogates tools;
they are not a substitute for proving the vendor CLI does not hand its auth
material to code it executes.

### 6.3 Beating the sandbox ceilings

The K8s sandbox imposes a ~305 s foreground-exec ceiling and a 1 h pod deadline,
and the cross-pod background-process manager is unreliable. The runner therefore
self-supervises (this is the exact workaround the sandbox study recommended):

1. One exec spawns the CLI detached: `nohup <cmd> > /run/code/run-<id>.log 2>&1 &`
   (writes its PID to a file). `<cmd>` should be a generated pod-local launcher
   path, not a shell string containing credentials. The runner uses the sandbox
   exec path directly, not the `terminal` tool, so it fully controls the command
   env.
2. A poll loop issues **short** (<305 s) exec reads that tail the new bytes of the
   log, parses freshly-emitted `stream-json` lines, emits **coalesced** progress
   events (never per-line — avoids event-log bloat), renews the session lease
   (interval < the 60 s lease TTL), and checks for interrupt.
3. On process exit, parse the final message + usage from the captured log.
4. Run the codex write-back (§6.2), then clean up the credential dir (optionally
   retain the log as a session artifact).

**Hard limit:** a single run is capped by the 1 h pod deadline. v1 documents this
and emits a clean error if the pod is reclaimed mid-run.

## 7. Harness `/code` Command

### 7.0 Prompt-injection exemption (API layer)

`send_message` (`api/routes/sessions.py:732`) screens every message body with
`PromptInjectionDetector.detect()` and raises **422 before persistence/enqueue**,
with no slash-command exemption. Coding prompts ("rewrite the old auth module")
routinely trip injection detectors, so v1 **exempts `/code` command text**: when
`body.content` is a `/code …` command, skip the body screen. This is safe because
the `/code` prompt is a command to the user's *own* coding agent in the user's
*own* sandbox — the builtin handler parses it and hands the prompt to the vendor
CLI; it is **never fed to the platform LLM**. Attachments and filenames remain
screened.

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

- `CodeCommandMixin` lives in `harness/loop_code_commands.py`, mixed into
  `AgentHarness`, following `OutcomeCommandMixin` for subcommand parsing, event
  emission, and store access.

### 7.2 Subcommands

| Subcommand | Behavior |
|---|---|
| `/code` / `/code help` | Usage text (emit an assistant-visible `LLM_RESPONSE`). |
| `/code status` | Connection status per provider. |
| `/code login <provider>` | Surface the paste-to-connect UI (the `CodingAgentsPanel` / a paste widget) with copy-paste instructions for `claude setup-token` / `codex login`. |
| `/code logout <provider>` | Delete the user-scoped bundle. |
| `/code claude\|codex <prompt> [flags]` | Run. Flags: `--model`, `--effort`, `--allow read-only`. If not connected → emit a "connect first" message pointing at `/code login`. |

Builtin handling early-returns (no main LLM call → deterministic, no token cost).
A handler that returns must always emit at least one assistant-visible event, or
the UI shows nothing for the turn.

### 7.3 Lifecycle correctness

- **Idempotency:** crash-recovery re-wakes re-process the *same* `user.message`
  event. Before launching, check whether a `CODE_RUN_STARTED` already exists whose
  **source user-event id** equals the current user event's id, and skip relaunch
  if so. (Key on the originating event id, **not** the message text — keying on
  text would wrongly suppress a user deliberately re-running the same prompt in a
  later turn.)
- **Lease:** the run can take minutes; renew the lease throughout (mirrors
  `ask_user_question`) so the orphan sweeper doesn't reclaim the session.
- **Interrupt:** on session pause, the poll loop detects the interrupt and kills
  the pod-side CLI process.

### 7.4 New event types (`session/events.py`)

`CODE_RUN_STARTED` (carries the source user-event id for idempotency),
`CODE_RUN_PROGRESS`, `CODE_RUN_RESULT`. Connection prompts use the paste widget
via the credential route, not the event log. Token-shaped data is redacted by
`emit_event`; regardless, **credentials must never be placed in event payloads**
(events stream verbatim to the browser over SSE).

## 8. SDK / Frontend (`agent-chat-react` + consumers)

- **Adapter methods** (all optional, runtime-probed, added to every consumer —
  `surogates/web`, `surogate-ops/frontend`, the example app, website-widget):
  `listCodingAgentConnections`, `submitCodingAgentCredential({provider, mode,
  value})`, `disconnectCodingAgentProvider({provider})`. (No popup/authorize/
  complete methods — capture A has no OAuth handshake.)
- **Composer:** add `/code claude `, `/code codex `, `/code login claude `,
  `/code login codex `, `/code status ` to `builtinCommands` (`chat-composer.tsx`),
  trailing-space convention. **Gated behind a new `codeAgentsEnabled` capability
  prop**, threaded exactly like `deepResearchEnabled`.
- **Connect UI:** a `CodingAgentsPanel` with a **paste form** per provider — short
  instructions ("run `claude setup-token`, paste the token"), a masked textarea,
  client-side format hint, submit via `submitCodingAgentCredential` (over the TLS
  credential route, never `sendMessage`), and a connected/not-connected status
  read from `listCodingAgentConnections`. No `openOAuthPopup`, no device-code
  display.
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
  `api.openai.com` (and `chatgpt.com` if Codex requires it) for the CLI's own API
  calls. Verify whether a restricting NetworkPolicy actually exists in the cluster.
- **Encryption:** `SUROGATES_ENCRYPTION_KEY` is already shared between the ops
  server and worker pods; the vault works as-is.
- **Cred dir:** a pod-local credential directory convention
  (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`) off the S3 mount.
- **Sandbox policy:** extend deny-read and threat-pattern coverage for
  `/run/code`, `auth.json`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR`; add regression
  tests that attempted reads are blocked and that no emitted event/log payload
  contains token-shaped strings.

## 10. Non-Goals (v1)

- Git-repo clone / `--repo` / `--work-dir` (runs operate on the session
  `/workspace` only).
- Agents other than `claude` and `codex` (no cursor/gemini/opencode/pi).
- Interactive mid-run Q&A (the agent pausing to ask the user a question).
- **Conversation resume** of any kind (each run is independent).
- A backend-driven OAuth/PKCE/device flow (capture A pastes binary-minted creds).
- Server-side token refresh for Claude (the `setup-token` is long-lived; codex
  self-refreshes in-pod with write-back).
- Detached/background jobs with status polling.
- Runs longer than the ~1 h pod deadline.
- Studio org-level visibility/management of `/code` credentials.
- headless-cli's roles/teams/run-graph orchestration and cron scheduling.

## 11. Risks & Open Questions

1. **Anthropic ToS / June-15 policy.** Compliant by running the real `claude`
   binary with a `setup-token` the binary minted. The 2026-06-15 "Agent SDK credit
   pool" change still needs product/legal sign-off.
2. **Long-lived Claude token at rest.** Capture A's `setup-token` is valid ~1 year,
   so a leak is high-impact. Mitigated by pod-local-off-S3 storage,
   process-launch-only injection, deletion after each run, and explicit
   `/run/code` deny patterns — but the blast radius is larger than a short-lived
   token. Security review required.
3. **Credential self-exposure inside vendor tool execution.** The vendor CLI must
   authenticate somehow, but the code it executes must not be able to read the
   subscription token, `auth.json`, or inherited provider env vars. This is a
   blocking preflight item for v1, because repository prompt injection could
   otherwise turn `/code` into a credential-exfiltration path.
4. **Codex credential staleness.** Codex `auth.json` access tokens go stale (~8
   days). The write-back in §6.2 keeps the vault copy fresh across runs; a user who
   doesn't run `/code codex` for longer than the window must re-paste. (Refresh
   token transiently in-pod is within OpenAI's documented `auth.json` portability.)
5. **Injection-exemption surface.** Exempting `/code` text from the detector is
   safe because the prompt never reaches the platform LLM, but the exemption must
   match **only** genuine `/code` commands (anchor on the same parse the harness
   uses) so it can't be used to smuggle un-screened content into a normal turn.
6. **s3fs performance.** Git-heavy coding agents on the FUSE-mounted `/workspace`
   may be slow; workspace-only v1 keeps repos small. Watch for FUSE quirks already
   documented in the sandbox.
7. **Pod deadline.** Long runs (>~1 h) are killed. Acceptable for v1; chunking is a
   future enhancement.

## 12. Implementation Phasing

1. **Auth foundation:** vault convention + `validate_pasted()` (`credentials.py`),
   the three credential routes, SDK adapter methods + `CodingAgentsPanel` paste
   form, and the `/code login/status/logout` subcommands. Ship "connect your plan"
   end-to-end before any execution.
2. **Execution:** `agents.py` (command builders + output parsing), `runner.py`
   (inject/spawn/poll/capture + codex write-back), the `/code claude|codex`
   subcommand, new event types + `CodeRunBlock`, sandbox image + NetworkPolicy,
   sandbox secret-read deny patterns, and the blocking vendor-CLI credential
   isolation preflight. Ship a streamed one-shot run only after the preflight
   proves the coding agent's tool subprocesses cannot read the injected auth
   material.
3. **Polish:** `--model` / `--effort` / `--allow` normalization, usage reporting,
   idempotency/interrupt hardening, env-hygiene tests, and docs
   (`docs/commands/index.md`, usage docs).

## 13. Review Trail

The first draft proposed a backend-driven OAuth/PKCE capture with server-side
refresh and `--session` resume. Spec review against the codebase surfaced:

- **Capture contradiction (blocking):** driving PKCE ourselves with Claude Code's
  public client is the third-party pattern Anthropic bans, contradicting §2.
  → Switched to capture **A** (paste a binary-minted credential).
- **Prompt-injection 422 (blocking):** the pre-harness detector
  (`sessions.py:732`) would block coding prompts. → **Exempt** `/code` text (§7.0).
- **Codex resume unverified (overpromise):** `codex exec` resume-by-id is not
  confirmed. → **Resume dropped** from v1 entirely.
- **Minor fixes folded in:** idempotency keyed on source event id (§7.3); env-vs-
  inline credential injection + env hygiene (§6.2); end-user-scoped credential
  route (§5.3).
