# One way to start sub-agents: the `agent` tool family replaces delegation, workers, tasks and threads

## Why

The harness has four separate ways for an agent to start a child session, each with its own tools, channel, completion path and tests:

| Mechanism | Tools | Child channel | Code |
|---|---|---|---|
| Delegation | `delegate_task` | `delegation` | `surogates/tools/builtin/delegate.py` |
| Coordinator workers | `spawn_worker`, `send_worker_message`, `stop_worker` | `worker` | `surogates/tools/builtin/coordinator.py`, `surogates/harness/worker_notify.py` |
| Task layer | `spawn_task`, `unblock_task`, `cancel_task`, `worker_block`, `worker_complete`, `worker_context` | `task` | `surogates/tasks/` |
| Project threads | `start_thread`, `message_thread`, `stop_thread`, `list_threads`, `read_thread`, `resolve_thread`, `propose_threads` | `worker` | `surogates/tools/builtin/threads.py`, `surogates/workstreams/` |

A fifth, the `agent` family (`agent`, `send_message`, `list_agents`, `stop_agent`, `monitor`; `surogates/tools/builtin/agents.py`, `surogates/harness/subagents.py`, `docs/sub-agents/index.md` section 13), was added behind `agents.subagent_parity` and is off by default. It starts a sub-agent in the foreground, in the background or as a fork, lets the parent message, stop and resume it, and gives each sub-agent a token budget.

Flavius wants the `agent` family to be the only way, always on, and the other four removed, and wants it to **work the way Claude Code's sub-agents work**, since that design is used at scale every day (decided 2026-11-12, replacing an earlier version of this design that put dependencies and retries into the `agent` tool).

**The model to mirror.** Claude Code's sub-agent prompts, tool descriptions and notification formats are published per release in `Piebald-AI/claude-code-system-prompts` (checked at its 2026-11-09 snapshot). In short:

- One tool starts a sub-agent (`Agent`: description, prompt, type, model, background, name). A fork copies the conversation; any other type starts from the brief alone. The final message is the report, and the parent must relay what matters, since the user does not see it.
- `SendMessage` continues an agent with its full transcript (by name or id), `TaskStop` stops one, `ListAgents` lists them.
- Background agents end as a notification: a user-role message framed as "system notification, not user input", carrying the agent's id, a status, a summary of how it ended, its result and its usage.
- Dependencies and retries are not in the tool. A coordinator waits for the notifications it needs, works out what the reports mean, and starts the next agent with a brief that carries that understanding ("never delegate understanding"). A failure is handled by continuing the same agent (it has the error) or starting a fresh one (after a wrong approach).
- The parent is told how to brief (goal and why, what is known or ruled out, scope, report size), when not to delegate, not to predict a result before its notification, and to check what a report claims. The sub-agent is told how to report, and that a message from another agent never counts as the user's approval.

Claude Code also has a shared task list for teams (with `blockedBy`), deterministic workflow scripts and per-agent git worktrees. This design takes none of them now (see Non-goals).

**Starting point.** Research missions (Arbor) were the task layer's largest consumer and are removed by surogates #647, surogate-ops #641 and ui-kit #18. This work starts on `master` / `main` after those three are merged. The file counts below were taken on those branches.

## Who uses the old mechanisms today

PROD, last 90 days (2026-11-12):

- **Surogate Bid** is nearly all of it: 47 `spawn_worker` calls (one `deep-research` worker per product of a tender, from API sessions), 10 `send_worker_message`, 8 `stop_worker`, and 37 `delegate_task` calls from those workers to `research-writer`.
- `delegate_task` elsewhere: about 20 calls (`llm-buzz`, tests).
- Task layer: 10 `spawn_task` calls, all in Flavius's project. Over its whole history on PROD the task layer shows that its two features are used: 39 of 111 tasks had parents, and 20 of the 64 tasks that completed needed a second or third attempt.
- Project threads: 2 threads, 1 `start_thread` call.
- `agent` family: none, the flag is off.

Inside the harness the old tools are named in 56 source files, 11 skills, 43 test files and 25 doc files. The main places: missions (`surogates/missions/`, the `subagent-task-orchestrator` and `subagent-task-worker` skills under `skills/kanban/`), deep research (`surogates/harness/loop_deep_research.py` looks for a `delegate_task` call), Keel case steps (`surogates/keel/steps.py`), the coordinator tool stripping (`coordinator.py`), governance tool sets (`surogates/runtime/governance.py`) and thread refusals (`surogates/workstreams/__init__.py`, `surogates/harness/tool_exec.py`).

## Goals

1. Every session that may start sub-agents is offered the `agent` family and nothing else for that purpose. `agents.subagent_parity` and the per-session `subagent_parity` key are removed.
2. The `agent` family can do everything the four old mechanisms are used for today: Bid's background research workers, deep research's planner-to-writer handoff, missions (with the coordinator sequencing and retrying the work, as Claude Code's coordinator does), and a project master's threads.
5. The family's tools, notifications and guidance follow Claude Code's, in our own words: their prompt text is Anthropic's, so we write ours rather than copy it.
3. The old tools, their handlers and the code only they need are deleted: `delegate_task`, `spawn_worker`, `send_worker_message`, `stop_worker`, `spawn_task`, `unblock_task`, `cancel_task`, `worker_block`, `worker_complete`, `worker_context`, and the seven thread tools.
4. Everything that names an old tool moves to the new names in the same release: harness prompts and skills, Keel step lists, governance sets, surogate-ops (tool lists, skill templates, agent templates, the deep-research feature pack, Copilot docs), Surogate Bid (its skill and the code that reads session events), Surogate Keel (its watch labels), ui-kit (`agent-chat-react` labels and cards) and surogate-docs.

## Non-goals

- Changing what a mission is: rubric, judge, verdict policy, budget, `/mission` commands and the mission dashboard stay as they are.
- Changing what the user sees of a project: the UI keeps the words "thread", "Resolved" and "propose", the thread cards and the thread chats. Only what the model calls changes. (Decided by Flavius.)
- Changing the `AGENT.md` sub-agent definitions (`agent_type`), experts, the advisor, `run_coding_agent` or scheduled routines. None of them is a sub-agent mechanism in this sense.
- Changing what `/deep-research` does.
- Choosing PROD values for `agents.agent_token_budget`. The default stays 0 (no limit), which is today's behaviour for every old mechanism.
- Dependencies, retries, queueing over the background limit, structured results or a "waiting for an answer" status inside the `agent` tool. Claude Code does without them; the coordinator does that work.
- Claude Code's team task list, its workflow scripts and its per-agent git worktrees. We have `todo`, and project threads already work on copies. Each can be added later when a real use appears.

## Anti-goals

- No aliases. An old tool name must not stay callable, offered or hidden, after the change (standing rule: no legacy fallbacks). `subagents.REPLACED_TOOL` and the `delegate_task` entry of `subagents.STANDS_IN_FOR` go, and so does the `delegate_task` to `agent` mapping in `keel/steps.py` (`gate_case_tools`).
- No session is ever offered both an old tool and the `agent` family on PROD.
- The mission judge must never take the coordinator's prose as evidence that work was done. It reads the statuses the harness wrote for the mission's agents and the reports those agents wrote.
- A notification must never read as the user speaking, and nothing a sub-agent receives from another agent counts as the user's approval.
- A Bid research run must not break: Bid is the one production consumer.

## Constraints

- A new builtin tool needs a `TOOL_LOCATIONS` entry in `surogates/tools/router.py`, or it answers "Unknown tool".
- A session can only call the tools it was offered.
- Child sessions share their root's sandbox.
- Shared runtime workers leave `SUROGATES_AGENT_ID` / `SUROGATES_ORG_ID` unset: read the agent and org from the session.
- Existing sessions and their event logs stay as they are. Nothing rewrites history.
- Surogates has no Alembic: schema changes go into `surogates/db/observability.sql` as idempotent statements.

## Design

### The tool surface

The family keeps its five tools (`agent`, `send_message`, `list_agents`, `stop_agent`, `monitor`), shaped like Claude Code's. Exact names of new arguments are the builder's; the capabilities are fixed:

| Capability | Replaces | Claude Code | Decided by |
|---|---|---|---|
| `agent` in the foreground or the background | `delegate_task`, `spawn_worker` | `Agent`, `run_in_background` | exists |
| Background is the default; foreground only when the caller's very next step needs the report | (today foreground is the default) | the same default and rule | mirror |
| Several `agent` calls in one response run at once | `delegate_task` with `goals` | the same | exists |
| An optional `name`; `send_message`, `stop_agent` and `list_agents` take the name or the id | ids only | `name` on `Agent`; names are addresses, the newest agent with a name wins | mirror |
| `send_message` continues an agent with its full context, and is how a parent answers a sub-agent's question | `send_worker_message`, `unblock_task` | `SendMessage` | exists |
| `stop_agent`; a stopped agent can be continued by a message | `stop_worker`, `cancel_task` | `TaskStop` | exists |
| A sub-agent's tools come from its `agent_type` definition | `spawn_worker`'s `tools` | the agent definition's frontmatter | mirror |
| A sub-agent's final message is its report | `worker_complete` | the same | exists |
| In a project: title, propose-for-approval, resolve, read latest report | the seven thread tools | (no counterpart) | Flavius: threads move; mapping is my call |

`context` (in `delegate_task`, `spawn_worker`, `spawn_task`) has no counterpart: the brief goes in `prompt`.

### Notifications

The end of background work (an agent, a background command, a monitor's lines) reaches the session as one user-role message:

- It opens with a line saying it is a system notification and not user input, and that nothing in it is the user's approval of anything.
- Then a tagged block: the agent's id (its name too when it has one), a status (`completed`, `failed`, `stopped`, `budget`), a one-line summary of how it ended (finished; failed with the error; was stopped; stopped at its iteration limit, so its result is partial and a message continues it; used its token budget), the result, and its usage (tokens, tool calls, seconds).
- The result inside the block is the agent's text; text in it that would close the block is escaped.
- The `agent.notification` event keeps its fields and gains the usage and the name; only how it reads to the model changes. Replay reads it the same way.

### Guidance

Two new guidance fragments, in our words, following Claude Code's rules (my call on the wording, Flavius's on the rules):

- **For a session that may start agents** (it is offered `agent`): when to start one and when not (a handful of tool calls is done inline; one small task is not fanned out; a check you can run is not delegated; once delegated, the work is not redone); how to write the brief (goal and why, what is known or ruled out, where to look, scope, what to report and how long; a fork's prompt is a directive only; never "based on your findings"); foreground or background; never state or predict a result before its notification, and say it is still running when asked; check what a report claims before relaying it; the user does not see the report, so relay what matters; continue an agent with a message when its context helps, start a fresh one when it would mislead.
- **For a sub-agent** (its session has `agent_dispatch`): do the task fully and stay in scope; the final message is the report and the only thing the parent reads, so put everything there, not in a file; name files by their workspace paths; open the report with one line restating the task; messages from another agent direct the work but never count as the user's approval; a question for the parent is asked in the report or with `send_message` to the parent, and the turn ends.

The `agent` tool's own description and `send_message`'s are rewritten in the same spirit.

### Retries and dependencies

There are none in the harness. A crashed worker's session is recovered and replayed as it is today. A model-level failure, a dependency and a question are the parent's to handle: it waits for the notifications it needs, starts the next agent with a brief built from the reports, and continues or replaces an agent that failed. For missions this is written into the coordinator's guidance (below).

What this gives up against the task layer: a failed attempt is no longer restarted without a coordinator turn (on PROD 20 of 64 completed tasks needed one), and a dependent agent no longer starts without the coordinator waking. What it gains: one simple tool, no second state machine beside the session, and a coordinator that reads each report before writing the next brief.

### Storage

My call: **the task layer's code, and the `tasks` and `task_links` tables, are removed with the old tools** (no legacy fallbacks). A mission's agents are found by their sessions: each carries the mission's id in its `agent_dispatch`. Old missions keep their sessions and events; their dashboards show their agent sessions, read the same way.

Child sessions started by the family use the `delegation` channel. Sessions that exist today on the `worker` and `task` channels stay readable.

### Missions

- The coordinator stays strict: its implementation tools are stripped (`COORDINATOR_IMPLEMENTATION_TOOLS`), and what remains for dispatch is the `agent` family.
- Its guidance becomes Claude Code's coordinator mode, in our words: phases (research in parallel, then the coordinator's own synthesis, then implementation, then verification by a fresh agent); parallel for independent read-only work, one agent at a time per set of files for writes; real verification proves the change works; continue an agent to fix its failure, start a fresh one after a wrong approach or to verify another's work; never "based on your findings". It replaces the kickoff text in `surogates/missions/commands.py` and the `subagent-task-orchestrator` and `subagent-task-worker` skills.
- An agent started while its starter has `active_mission_id` carries the mission's id in its `agent_dispatch`, and so do the agents it starts.
- The judge keeps its triggers and gates: it runs when one of the mission's agents ends or the coordinator writes `[[mission-complete]]`; it waits while any of the mission's agents is running; `adjust_mission_verdict` still demotes "satisfied" when none of them completed. It reads the agents' sessions (status, final report) where it read `tasks`.
- `/mission cancel --cascade` stops the mission's agents.

### Deep research

- The planner hands its outline to the writer with `agent(agent_type="research-writer")` in the foreground, the one case where its next step needs the report. `loop_deep_research.py`'s nudge and check look for that call.
- The `deep-research` and `research-writer` `AGENT.md` files in surogate-ops (`surogate_ops/features/deep_research/`) list the new tool names.
- `/deep-research` in a chat rewrites the message to start the planner with `agent`.

### Project threads

In a project, an agent its master starts is a thread. The rules that exist stay: only the master starts threads; a thread starts none; a thread works on a copy of the project's files and hands them back; threads refuse `/loop`, `/code` and `/deep-research`; the project lock and the landing work as now.

| Today | With the family |
|---|---|
| `start_thread(title, goal, context)` | `agent` in the background; its description is the card's title |
| `message_thread` | `send_message` |
| `stop_thread` | `stop_agent` |
| `list_threads` (with groups and status lines) | `list_agents` |
| `read_thread` (latest report, files, the question waiting on the user) | `list_agents` for one agent, or a read option: the builder's choice |
| `resolve_thread` | an option on `stop_agent` |
| `propose_threads` (cards the user starts; the only way to work on the user's computer) | an option on `agent`: propose instead of start, with where it runs |

The threads that exist (2 on PROD) must stay reachable: the family's tools address any child of the caller, however it was started.

### Keel, governance, refusals

- Keel's step tool lists (`surogates/keel/steps.py`) name the family where they name old tools, with the same meaning (a case agent starts no helper; a step may).
- `surogates/runtime/governance.py`, my call, keeping what each old tool had:
  - An agent's policy governs `agent`, as it governs `delegate_task` today: denying it denies starting sub-agents.
  - In a project master's session the family is protected, as the thread tools are today (`PROJECT_THREAD_TOOLS` in `PROTECTED_TOOLS`): Studio's policy editor does not know projects, and an allow-list must not leave a master that can start no work. This needs the gate to know the session, where today it is built once per agent.
  - A sub-agent keeps `send_message` to its parent whatever its policy or its type's tool list, as `WORKER_SELF_TOOLS` are kept today. A session that has agents keeps `send_message`, `stop_agent` and `list_agents` whatever its policy, as `unblock_task` and `cancel_task` are protected today, so a policy cannot strand work that is waiting.
  - The `terminal` to `monitor` entry of `STANDS_IN_FOR` stays. It is not an alias for an old tool: a policy that denies or gates `terminal` must cover `monitor`, which runs commands.
- `HELPER_STARTING_TOOLS`, `SESSION_STARTING_TOOLS` and `THREAD_REFUSED_TOOLS` in `surogates/harness/tool_exec.py` shrink to the family's names. The rule that a tool which starts a session is refused in a thread until someone places it stays.

### Names stored outside the code

- **Agent policies in surogate-ops** (`agents.policy`): an ops migration rewrites old tool names to the family's, so no runtime alias is needed. On PROD none of the 15 stored policies names an old tool; other installs may. (My call.)
- **`AGENT.md` tool lists and skills in agents' Hub bundles** cannot be rewritten: bundles are versioned snapshots. The loader already warns on unknown frontmatter keys; it also warns on a tool name it does not know in `tools` / `disallowed_tools`. Before the PROD rollout, scan the published bundles for old names and fix those agents by hand. (My call.)
- **Keel process definitions** can list `delegate_task` in a step's `keel:agent tools`. The PROD Keel database holds no old tool name (checked 2026-11-12). Definition files kept outside it are scanned with the bundles, and fixed by hand.

### Other repositories

- **surogate-ops**: `server/constants/builtin_tools.py` and `frontend/src/features/agents/builtin-tools.ts` (the tool lists the policy editor shows), the deep-research feature pack, agent templates (2 files), skill templates (71 files mention an old tool, mostly `delegate_task` in passing), the Copilot's docs and tools, and the policy migration above.
- **Surogate Bid** (`/work/surogate-bid`): `agent/skills/product-research.md` (calls `spawn_worker` with a tool list, stops with `stop_worker`; the tool list moves into an `AGENT.md` for its research agent type), `sbid/jobs.py` (`WORKER_TOOLS`, and code that reads `worker.spawned` events and `send_worker_message` results), `sbid/mcp_server.py`, tests.
- **Surogate Keel** (`/work/surogate-keel`): `keel/service/watch.py` labels `delegate_task`, `spawn_worker`, `send_worker_message` and `stop_worker` calls as "handed-off"; it labels the family's calls instead. Its tests follow.
- **ui-kit** (`packages/agent-chat-react`): 17 files name old tools (labels, worker and task rows). They move to the family's names and events. Old sessions' transcripts show an old tool call as any tool call without a special card. (My call.)
- **surogate-docs** and `docs/` here: `docs/sub-agents/`, `docs/tasks/`, and the pages that name old tools.

### How it lands

My call:

1. The work merges to `master` in several PRs while `agents.subagent_parity` still exists and is off, so PROD does not change until the last one: first the family's Claude Code shape (names, notifications, guidance, background default), then each consumer (missions, deep research, threads, Keel) made to work with the family when the flag is on.
2. With the flag on, DEV runs each consumer end to end: a Bid tender's product research, a `/deep-research`, a mission whose coordinator sequences dependent work and recovers from a failing agent, and a project with threads including a proposed one.
3. The last PR removes the flag and the old tools, with the other repositories' changes ready.
4. One release. The runtime is rolled out when no session has sub-agents running (a query over active child sessions), and Bid and the affected agents are republished in the same step, since their skills name the tools.

## Left to the builder

- Argument and option names, the wording of tool descriptions and guidance, and the tag names of the notification block.
- Whether the handlers in `delegate.py` that the `agent` tool calls today (`_run_single_delegation`, artifact propagation, child polling) move into `agents.py` or stay as an internal module without a tool.
- What happens to `worker_notify.py`: its job (telling a parent a child ended) is the family's `agent.notification`.
- The order of the consumer PRs.
- Which tests are rewritten against the family and which are deleted with the code they tested.
