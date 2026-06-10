# Coding Crew on the Kanban Board — Design

**Status:** Approved (brainstorming) — pending implementation plan.

## 1. Goal & Motivation

Showcase the `/code` coding-agent capability as a *team* the platform manages,
not a single command a human types. A user gives one build goal to an
**orchestrator** agent; the orchestrator decomposes it into a durable kanban
DAG and routes the cards to two specialist sub-agents — **Claude Code** for
implementation and **Codex** for review + tests — that work on a shared
workspace, loop through review→fix, and finish with a working app. Everything
runs on the *user's own* Claude/ChatGPT plans.

The headline for users: **two best-in-class coding agents, coordinated as a
visible crew by a Surogate, on your own plans.**

## 2. The Controlling Insight

The platform already provides almost everything:

- The **subagent task layer** (`spawn_task`, `parents=[...]` DAG, `unblock_task`,
  `worker_block`, retry-with-history, the board UI) — `docs/tasks/index.md`.
- The **orchestrator** and **worker** skills — `skills/kanban/subagent-task-orchestrator`
  and `skills/kanban/subagent-task-worker`.
- **Workspace inheritance:** task-backed worker sessions reuse the spawning
  parent's `workspace_path` + `sandbox_root_session_id` (`docs/tasks/index.md`
  §"Workspace inheritance"). So Claude implements in `/workspace` and Codex
  reviews the *same files in the same sandbox pod*.
- The `/code` execution engine (credential resolution, sandbox launch/poll,
  `CODE_RUN_*` events, `CodeRunBlock`) already shipped.

The **only missing primitive** is an LLM-callable tool that runs a coding agent.
`/code` is a user slash command parsed in `wake()`, not a tool — so a sub-agent
LLM cannot invoke it. This spec adds that one tool and the catalog presets that
turn it into a crew.

## 3. New Primitive: the `run_coding_agent` tool

A worker-local, side-effecting tool (mirrors `consult_expert` / `delegate_task`):

```
run_coding_agent(
    agent: "claude" | "codex",
    prompt: str,
    model?: str,
    effort?: "low" | "medium" | "high" | "xhigh",
    read_only?: bool,
) -> { final_message: str, input_tokens: int, output_tokens: int, error?: str }
```

Behavior:
- Resolves the calling session's connected plan via `tenant.user_id` (the same
  per-user vault lookup the slash command uses; missions/tasks carry a durable
  human owner, so this works in autonomous contexts).
- Ensures the session sandbox, drives the existing runner, emits
  `CODE_RUN_STARTED/PROGRESS/RESULT` (so each run still renders as a
  `CodeRunBlock` in the thread), and returns the final message + token usage to
  the calling LLM as the tool result so it can act on it.
- **Sequential by design.** Claude and Codex share one `/workspace`; the tool is
  not parallel-safe within a session (concurrent writes would race). The
  implement→review→fix flow is naturally serial, and cross-agent parallelism is
  expressed at the *task* layer (independent cards), not within one session.
- Not connected → returns a clean error the orchestrator can surface or route to
  a human via `worker_block`.

**Shared-core refactor.** Extract the run core currently inside
`surogates/harness/loop_code_commands.py::_run_code_agent` (load bundle → build
invocation → ensure sandbox → run runner → emit `CODE_RUN_*` → codex write-back)
into one shared callable used by **both** the `/code` slash handler and the new
tool. No behavior change to the slash path.

Idempotency: the tool relies on standard tool-call replay (a `TOOL_RESULT` after
the `TOOL_CALL` means done) rather than the slash path's source-event-id guard.

## 4. Catalog Presets (configuration, not engine code)

Three AgentDefs (`name`, system prompt, tool filter, model):

- **`claude-coder`** — tool filter allows `run_coding_agent` (+ read-only file
  inspection); prompt: "Implement the assigned task by calling
  `run_coding_agent(agent='claude', …)`. Report changed files + how you verified."
- **`codex-reviewer`** — tool filter allows `run_coding_agent`; prompt: "Review
  the implementation in `/workspace` and run its tests by calling
  `run_coding_agent(agent='codex', …)`. If you find issues, `worker_block` with a
  `review-required:` reason describing them; otherwise `worker_complete`."
- **`code-orchestrator`** — `spawn_task`/`unblock_task`/`cancel_task` enabled,
  implementation tools stripped, runs `subagent-task-orchestrator`. Its
  "# Available Sub-Agents" roster lists `claude-coder` and `codex-reviewer`.

Sub-agents inherit the parent's workspace, skills, and connected coding plans.

## 5. Orchestration Flow (existing machinery)

The orchestrator decomposes a build goal into:

```
implement (claude-coder)
  → review (codex-reviewer, parents=[implement])      # runs tests, blocks on issues
  → fix (claude-coder, parents=[review])              # only if review blocked
  → verify (codex-reviewer, parents=[fix])
```

The review→fix loop is the task layer's native `worker_block`/`unblock_task`
cycle: `codex-reviewer` blocks with `review-required: …`; the orchestrator spawns
a `fix` card carrying that context to `claude-coder`; on the next review the
findings are resolved. No new control-flow code.

## 6. The Demo

1. User connects both plans (Settings → Coding Agents).
2. User gives `code-orchestrator` one goal: *"Build a working URL-shortener
   (small Flask API + one HTML page) in the workspace — implemented, reviewed,
   and tested."*
3. On screen: the board fills — `implement` → in-progress (`CodeRunBlock` streams
   Claude writing files) → `review` picks up (`CodeRunBlock` of Codex running the
   tests, then a block with findings) → `fix` routes back to Claude → `verify`
   goes green → board complete, working app in `/workspace`.

The visible artifacts — cards flowing across lanes, two distinct `CodeRunBlock`s,
the shared workspace tree filling up, a green board — are the showcase.

**Demo task:** the URL-shortener (small enough to finish live, real enough to
impress). Swappable, but the task should be one-pod-sized and have a runnable
test suite so `verify` is meaningful.

## 7. Non-Goals (v1 of this demo)

- Parallel coding within a single session/workspace (race on `/workspace`).
- Conversation resume of a vendor CLI mid-run (still one-shot per `run_coding_agent`
  call).
- Interactive mid-run approval/Q&A from the vendor CLI (unchanged — still
  bypass/yolo or read-only).
- A bespoke board UI for code runs — reuse the existing task board + `CodeRunBlock`.
- Productizing the AgentDef catalog in Studio UI (presets can be seeded for the
  demo; polished management is later).

## 8. Risks & Open Questions

1. **Live runtime length.** Real claude+codex runs across a 4-card DAG take
   minutes. Acceptable (narrate while it runs); pre-staging a connected agent +
   warm sandbox helps. Mitigate by keeping the task one-pod-sized.
2. **Sub-agent reliability.** `spawn_task` to an unknown `agent_type` sits
   forever — the three AgentDefs must be present in the orchestrator's roster and
   spelled exactly.
3. **Credential presence in autonomous context.** The human owner's plan must be
   connected before the run; `run_coding_agent` returns a clean "not connected"
   error otherwise, which the orchestrator should surface rather than spin.
4. **Vendor-CLI isolation preflight** (from the execution spec, §6.2/§11.3) still
   gates trusting this with real subscription credentials on a shared cluster.
5. **Tool-call idempotency on crash-recovery.** A worker crash mid-run could
   re-invoke `run_coding_agent`; rely on task retry-with-history + the standard
   tool replay guard; document the re-run window.

## 9. Implementation Phasing

1. **Bridge:** shared-core refactor + `run_coding_agent` tool + tests (slash path
   unchanged; tool exercised via fakes like the existing runner tests).
2. **Presets:** seed `claude-coder`, `codex-reviewer`, `code-orchestrator`
   AgentDefs (prompt + tool filter + roster).
3. **Demo dry-run:** connect plans, run the URL-shortener goal end to end on the
   dev cluster (also serves as the vendor-CLI isolation check), capture the board
   + `CodeRunBlock` screenshots for the showcase.
