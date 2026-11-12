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

Flavius wants the `agent` family to be the only way, always on, and the other four removed.

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
2. The `agent` family can do everything the four old mechanisms are used for today: Bid's background research workers, deep research's planner-to-writer handoff, missions with dependencies and retries, and a project master's threads.
3. The old tools, their handlers and the code only they need are deleted: `delegate_task`, `spawn_worker`, `send_worker_message`, `stop_worker`, `spawn_task`, `unblock_task`, `cancel_task`, `worker_block`, `worker_complete`, `worker_context`, and the seven thread tools.
4. Everything that names an old tool moves to the new names in the same release: harness prompts and skills, Keel step lists, governance sets, surogate-ops (tool lists, skill templates, agent templates, the deep-research feature pack, Copilot docs), Surogate Bid (its skill and the code that reads session events), Surogate Keel (its watch labels), ui-kit (`agent-chat-react` labels and cards) and surogate-docs.

## Non-goals

- Changing what a mission is: rubric, judge, verdict policy, budget, `/mission` commands and the mission dashboard stay as they are.
- Changing what the user sees of a project: the UI keeps the words "thread", "Resolved" and "propose", the thread cards and the thread chats. Only what the model calls changes. (Decided by Flavius.)
- Changing the `AGENT.md` sub-agent definitions (`agent_type`), experts, the advisor, `run_coding_agent` or scheduled routines. None of them is a sub-agent mechanism in this sense.
- Changing what `/deep-research` does.
- Choosing PROD values for `agents.agent_token_budget`. The default stays 0 (no limit), which is today's behaviour for every old mechanism.

## Anti-goals

- No aliases. An old tool name must not stay callable, offered or hidden, after the change (standing rule: no legacy fallbacks). `subagents.REPLACED_TOOL` and the `delegate_task` entry of `subagents.STANDS_IN_FOR` go, and so does the `delegate_task` to `agent` mapping in `keel/steps.py` (`gate_case_tools`).
- No session is ever offered both an old tool and the `agent` family on PROD.
- A dependent agent must never start while one of its dependencies is failed, stopped or waiting for an answer.
- The mission judge must never take the coordinator's prose as evidence that work was done. It reads statuses the harness wrote and results the sub-agents recorded.
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

The family keeps its five tools. `agent` gains arguments; the others gain small options. Exact argument names are the builder's, the capabilities are fixed:

| Capability | Replaces | Decided by |
|---|---|---|
| `agent` in the foreground | `delegate_task` with one goal | exists |
| Several `agent` calls in one response (up to 5) | `delegate_task` with `goals` | exists |
| `agent` in the background, `send_message`, `stop_agent`, `list_agents` | `spawn_worker`, `send_worker_message`, `stop_worker` | exists |
| `agent` with a per-call tool allowlist | `spawn_worker`'s `tools` (Bid passes one) | my call |
| `agent` that starts only after named agents completed | `spawn_task`'s `parents` | Flavius: add it |
| `agent` with a number of retries | `spawn_task`'s `max_attempts` | Flavius: add it |
| A sub-agent records structured data with its result | `worker_complete`'s `metadata` | my call |
| A sub-agent says it is waiting for an answer from its parent | `worker_block`, `unblock_task` | my call |
| Stopping an agent that has not started yet | `cancel_task` | follows from the above |
| In a project: title, propose-for-approval, resolve, read latest report | the seven thread tools | Flavius: threads move; mapping is my call |

`context` (in `delegate_task`, `spawn_worker`, `spawn_task`) has no counterpart: the brief goes in `prompt`.

### Dependencies

- An agent started with dependencies is created at once: it has an id, shows in `list_agents` as waiting, and can be stopped. It starts when every agent it depends on has completed.
- A dependency that fails, is stopped, or runs out of retries does not release its dependents. The agent that started them is told (as an `agent.notification`), and decides: stop them or start replacements.
- Dependencies need the background mode: a foreground call cannot wait for other agents.
- When a waiting agent starts, the reports of the agents it depended on are put in front of its brief. This replaces `worker_context`. (My call.)
- A waiting agent does not count toward `MAX_RUNNING_IN_BACKGROUND` (5). An agent that would exceed the cap waits for a free slot the same way, instead of being refused: a mission that starts eight independent pieces of work is accepted today by the task layer and must stay accepted. (My call.)
- The wait must survive a worker's death. The task layer's dispatcher tick (`surogates/tasks/dispatcher.py`: promote, finalize, enqueue, every 5 s from the orchestrator) does this today and is the place to keep doing it.

### Retries

My call, since Flavius left it to me: **a retry is a fresh agent started from the original brief, with a summary of the earlier attempts in front of it.** This is what the task layer does today (`surogates/tasks/spawn.py`, `fetch_prior_attempt_summaries`), and on PROD it turned 20 failed first attempts into completed tasks. A fresh start is also the only retry that helps when the failed conversation itself is the cause: a context that keeps compacting, or a history a provider now rejects.

Continuing a failed agent with its conversation stays possible by hand: the agent that started it sends it a message, which resumes it (this exists).

- A retry is used when a run ends in failure or dies without ending. A stop by the parent or the user is not retried. An agent that is waiting for an answer has not failed and uses no retry.
- The id the parent holds for an agent stays valid across its attempts: `send_message`, `stop_agent`, `list_agents` and other agents' dependencies keep working after a retry.
- The token budget of an agent covers all its attempts.
- The default is no retries, as for `agent` today. The mission coordinator's guidance asks for retries where the task layer's default of 3 attempts applied.

### Results and waiting for an answer

- A sub-agent's final message is its report, as in the family today.
- A sub-agent can record structured data with its result (for example a verifier's counts). It reaches the parent in the notification and is stored where the mission judge reads it. The parent cannot write it.
- A sub-agent that needs an answer from its parent says so explicitly and ends its turn. Its status is then "waiting": it is not completed (its dependents stay waiting) and not failed (no retry). The parent's `send_message` resumes it with the answer.

### An agent that runs again

The family already lets the agent that started a sub-agent resume it with a message once it has completed, failed or been stopped, and its guidance tells the model to do so for follow-up work. With a record in `tasks`, that has to be defined. My call: **the record is re-opened.**

- The record goes back to running. When the new run ends, its report and recorded result replace the earlier ones. A verifier asked to verify again must not leave its old counts for the judge.
- Dependents wait while a dependency is anything but completed, and start once all of them are completed, including one that completed after being resumed. Dependents that had already started are not touched by a later resume.
- For a mission, a resumed agent is work in flight again, and its end is an end state like any other: the judge runs again.
- A run resumed by a message is not retried automatically. A fresh start from the original brief would drop the follow-up that resumed it, so its failure is reported to the parent, who decides.

### Storage

My call: **keep the `tasks` and `task_links` tables as the record of an agent that has dependencies, retries, or a mission.** Reasons: the dispatcher tick over them is race-safe and already tested; the mission judge (`surogates/missions/evaluator.py`, `verdict_policy.py`), the missions API and the dashboard's Tasks tab read them and can stay as they are; and existing rows stay valid history. A plain `agent` call with none of the three needs no row and works as it does today.

Whether the id the model sees is the row's id or something else is the builder's, as long as the rule above holds (one id, valid across attempts).

Child sessions started by the family keep one channel. Sessions that exist today on the `worker` and `task` channels stay readable; no new ones are created there unless the builder finds a reason threads must keep theirs (queue priority, `surogates/channels/constants.py`).

### Missions

- The coordinator stays strict: its implementation tools are stripped (`COORDINATOR_IMPLEMENTATION_TOOLS`), and what remains for dispatch is the `agent` family.
- The kickoff text (`surogates/missions/commands.py`) and the `subagent-task-orchestrator` skill describe the work in the family's terms: background agents, dependencies, retries, a verifier agent whose recorded result carries the measurable signal. `subagent-task-worker` becomes the guidance for an agent started inside a mission, or is folded into the family's own sub-agent guidance.
- An agent started while its starter has `active_mission_id` is linked to the mission, as `spawn_task` does today, and so are agents those agents start.
- The judge's triggers and gates keep today's behaviour exactly. It runs when a mission's agent reaches an end state or the coordinator writes `[[mission-complete]]`. It is held back only by work that will finish with no one acting (`any_task_in_flight` in `surogates/missions/store.py`): agents that are running or about to start. An agent waiting for an answer, and an agent stuck behind a dependency that failed, was stopped or is waiting, do not hold it back, as `blocked` tasks and their children do not today. The judge's prompt lists them, so its verdict can send the coordinator back to them, and `nudge_idle_missions` keeps nudging a mission with nothing moving. `adjust_mission_verdict` still demotes "satisfied" when nothing completed.
- `/mission cancel --cascade` stops the mission's agents.

### Deep research

- The planner hands its outline to the writer with `agent(agent_type="research-writer")` in the foreground. `loop_deep_research.py`'s nudge and check look for that call.
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
  - What a sub-agent needs to report its own state (recording its result, saying it is waiting, messaging its parent) is protected, as `WORKER_SELF_TOOLS` are. A session that has agents keeps `send_message`, `stop_agent` and `list_agents` whatever its policy, as `unblock_task` and `cancel_task` are protected today, so a policy cannot strand work that is waiting.
  - The `terminal` to `monitor` entry of `STANDS_IN_FOR` stays. It is not an alias for an old tool: a policy that denies or gates `terminal` must cover `monitor`, which runs commands.
- `HELPER_STARTING_TOOLS`, `SESSION_STARTING_TOOLS` and `THREAD_REFUSED_TOOLS` in `surogates/harness/tool_exec.py` shrink to the family's names. The rule that a tool which starts a session is refused in a thread until someone places it stays.

### Names stored outside the code

- **Agent policies in surogate-ops** (`agents.policy`): an ops migration rewrites old tool names to the family's, so no runtime alias is needed. On PROD none of the 15 stored policies names an old tool; other installs may. (My call.)
- **`AGENT.md` tool lists and skills in agents' Hub bundles** cannot be rewritten: bundles are versioned snapshots. The loader already warns on unknown frontmatter keys; it also warns on a tool name it does not know in `tools` / `disallowed_tools`. Before the PROD rollout, scan the published bundles for old names and fix those agents by hand. (My call.)
- **Keel process definitions** can list `delegate_task` in a step's `keel:agent tools`. The PROD Keel database holds no old tool name (checked 2026-11-12). Definition files kept outside it are scanned with the bundles, and fixed by hand.

### Other repositories

- **surogate-ops**: `server/constants/builtin_tools.py` and `frontend/src/features/agents/builtin-tools.ts` (the tool lists the policy editor shows), the deep-research feature pack, agent templates (2 files), skill templates (71 files mention an old tool, mostly `delegate_task` in passing), the Copilot's docs and tools, and the policy migration above.
- **Surogate Bid** (`/work/surogate-bid`): `agent/skills/product-research.md` (calls `spawn_worker` with a tool list, stops with `stop_worker`), `sbid/jobs.py` (`WORKER_TOOLS`, and code that reads `worker.spawned` events and `send_worker_message` results), `sbid/mcp_server.py`, tests.
- **Surogate Keel** (`/work/surogate-keel`): `keel/service/watch.py` labels `delegate_task`, `spawn_worker`, `send_worker_message` and `stop_worker` calls as "handed-off"; it labels the family's calls instead. Its tests follow.
- **ui-kit** (`packages/agent-chat-react`): 17 files name old tools (labels, worker and task rows). They move to the family's names and events. Old sessions' transcripts show an old tool call as any tool call without a special card. (My call.)
- **surogate-docs** and `docs/` here: `docs/sub-agents/`, `docs/tasks/`, and the pages that name old tools.

### How it lands

My call:

1. The work merges to `master` in several PRs while `agents.subagent_parity` still exists and is off, so PROD does not change until the last one: first the new capabilities of the family, then each consumer (missions, deep research, threads, Keel) made to work with the family when the flag is on.
2. With the flag on, DEV runs each consumer end to end: a Bid tender's product research, a `/deep-research`, a mission with dependencies and a failing step that retries, and a project with threads including a proposed one.
3. The last PR removes the flag and the old tools, with the other repositories' changes ready.
4. One release. The runtime is rolled out when no session has sub-agents running (a query over active child sessions), and Bid and the affected agents are republished in the same step, since their skills name the tools.

## Left to the builder

- Argument and option names, the wording of tool descriptions and guidance.
- How an agent's id is represented when it has a row in `tasks`.
- Whether the handlers in `delegate.py` that the `agent` tool calls today (`_run_single_delegation`, artifact propagation, child polling) move into `agents.py` or stay as an internal module without a tool.
- What happens to `worker_notify.py`: its job (telling a parent a child ended) is the family's `agent.notification`.
- The exact shape of the "waiting for an answer" signal and of the recorded result.
- The order of the consumer PRs.
- Which tests are rewritten against the family and which are deleted with the code they tested.
