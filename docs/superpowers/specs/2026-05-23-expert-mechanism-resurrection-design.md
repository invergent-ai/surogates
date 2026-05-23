# Expert Mechanism Resurrection — Design

**Date:** 2026-05-23
**Status:** Draft, pending implementation plan

## Background

`docs/experts/index.md` describes a working expert mechanism: model-backed
skills the base LLM consults for specialist tasks, with auto-routing on hard
tasks, auto-disable on quality regression, and a training-data pipeline.

A code review (review session, 2026-05-22) found the runtime side dangling:

- `consult_expert` builtin is defined but never registered in
  `surogates/tools/runtime.py` — the LLM never sees its schema.
- The harness-forced auto-router (`select_expert_for_task`,
  `load_skills_for_expert_routing`, `classify_tool_calls`) is no longer
  called by `harness/loop.py`; its slot was taken over by the hidden advisor,
  leaving the old helpers and forced-route compatibility shims unused.
- No code path injects an `# Available Experts` section into the system
  prompt.
- `EXPERT_DELEGATION`, `EXPERT_RESULT`, `EXPERT_FAILURE` events have no
  emitters in production code.
- The training collector's `collect_for_expert` path returns `[]` in practice
  because no `expert.delegation` events are ever written.
- Auto-disable in `record_expert_outcome._update_db_stats` is unreachable.

The CRUD/lifecycle endpoints (`/v1/skills/{name}/activate|retire|collect`),
the storage layer, the `Skill` table's expert columns, the SQL view
`v_expert_outcomes`, the feedback endpoint, and the `collect_for_skill`
bootstrap path remain live but are useless without runtime delegation.

This spec resurrects the runtime side as a **voluntary consultation**
mechanism — no auto-routing, no auto-disable.

## Goals

1. The base LLM can call `consult_expert(expert, task, context?)` and receive
   the expert's deliverable as a tool result.
2. Users can invoke an expert via `/<expert-name> <task>`; the deliverable
   flows back through the base LLM for review and relay.
3. Active experts are discoverable via the system prompt and via
   `skills_list`.
4. Every consultation emits `expert.delegation` and `expert.result` /
   `expert.failure` events so the SQL views, training collector, and
   feedback API become useful.
5. The dead auto-router code and forced-route compat shims are excised.

## Non-Goals

- **No auto-routing.** The harness does not preempt the base LLM with an
  expert based on hard-task classification. The advisor keeps its current
  role unchanged.
- **No auto-disable.** Operators retire experts manually via
  `POST /v1/skills/{name}/retire`. Telemetry events still fire so consumers
  can compute quality signals externally.
- **No vault-backed expert credentials.** Endpoint API keys keep coming
  from `tenant.org_config["expert_api_keys"]` (or
  `expert_api_key` for a global fallback). Vault integration is a follow-up.
- **No sub-sandbox isolation.** The expert mini-loop reuses the parent
  session's tool router and sandbox. The expert's `tools:` whitelist still
  constrains which tools the mini-loop may call.
- **No DB schema migrations.** The `Skill.expert_stats` JSONB column stays
  but is never written. Removing it is a follow-up.

## Design

### Vocabulary

`delegate_task` is the sub-agent / worker delegation tool — distinct from
expert consultation. To avoid confusion this spec uses:

- **consult** — what happens when the base LLM or a user invokes an
  expert (mini-loop runs, returns deliverable).
- **specialty** — the user-facing label for an expert's `trigger:`
  frontmatter field in the prompt section. The frontmatter key stays
  `trigger:`.
- **delegate / sub-agent / sub-task** — reserved for `delegate_task`.

### Invocation surface

Two voluntary entry points. Both converge on the existing
`ExpertConsultationService.consult()`
(`surogates/tools/builtin/expert_service.py:50`), which emits the
`expert.delegation` event, runs `run_expert_loop`, and emits
`expert.result` or `expert.failure`.

| Entry point | Trigger | Caller | Result handling |
|---|---|---|---|
| **A. Slash command** | User types `/sql_writer <task>` | `slash_skill.expand_slash_skill` detects `type=expert` and active status, calls the service. | Deliverable injected as a synthetic user message of shape `[Expert sql_writer delivered:]\n{deliverable}\n\nUser request: {args}`. Base LLM sees the synthetic message in the same turn and reviews/relays. |
| **B. LLM tool call** | Base LLM emits `consult_expert(expert, task, context?)` | Existing handler at `surogates/tools/builtin/expert.py:77` calls the service. | Deliverable returned as the tool result string. Base LLM continues normally. |

### Discoverability for the LLM

Three reinforcing mechanisms:

1. **Tool registered and routed.** `surogates/tools/runtime.py:50-90`
   gets the `expert` module added to the imports and to the `modules` list,
   so `register(self.registry)` runs at startup and `consult_expert`'s schema
   is in every chat completion request. `surogates/tools/router.py` also
   maps `"consult_expert"` to `ToolLocation.HARNESS`; otherwise the default
   sandbox fallback would expose the schema but fail execution.
2. **`skills_list` exposes expert metadata.** The local
   `_skills_list_handler` response entries include `type`, `trigger`, and,
   for experts, `expert_status`, `expert_model`, and `expert_endpoint`,
   matching the `/v1/skills` API shape that `HarnessApiClient.list_skills`
   already passes through. One sentence is also appended to
   `SKILLS_LIST_SCHEMA.description` in
   `surogates/tools/builtin/skills.py:59-72`: *"Entries with `type: expert`
   are specialist models; consult active experts via
   `consult_expert(expert, task)` rather than `skill_view`."*
3. **`# Available Experts` prompt section.** The harness `PromptBuilder`
   appends a section built from
   `get_active_experts(loaded_skills)`
   (`surogates/tools/builtin/expert.py:68`). The section is omitted
   entirely when no active experts exist.

   Format:
   ```
   # Available Experts

   Specialist models you can consult for focused domain work. Call
   `consult_expert(expert, task)` when a request falls within an expert's
   specialty — for example, a SQL writer for query-shaped questions or
   a code reviewer for inspecting a file. Do NOT use `delegate_task`
   for this — that tool spawns sub-agents for multi-step work in a
   fresh session; experts are single-shot specialists.

   - **sql_writer** — Writes PostgreSQL queries from natural language descriptions
     Specialty: SQL queries, database schemas, PostgreSQL, data analysis
   - **code_reviewer** — Reviews Python code for bugs, security, style
     Specialty: Python code review, bugs, security issues, style violations
   ```

### Tool schema description

`surogates/tools/builtin/expert.py:28-32` currently reads:

> "Delegate a subtask to a task-specialized expert model. The expert
> handles the subtask and returns its result. Use this when a task falls
> within an available expert's specialty."

It changes to:

> "Consult a specialist model for a single domain question. The expert
> answers and returns its deliverable. Use this when a request falls
> within an available expert's specialty."

This removes the `delegate` / `subtask` collision with `delegate_task`.

### Slash-command branching

`surogates/harness/slash_skill.py` gains an expert branch. Its
`expand_slash_skill` signature grows service dependencies from the harness
call site: `session_store`, `sandbox_pool`, and the existing tool registry
passed as `tools`. The service can build its fallback `ToolRouter` from
`sandbox_pool` when no router object is passed. After
`parse_slash_command` returns `(name, args)`:

1. Load the tenant skill catalog with the same loader path used by
   `consult_expert` fallback loading, using `session_factory` when present.
   This branch must happen before `skill_view` dispatch so experts are not
   accidentally treated like prompt skills.
2. If the skill exists and `skill.is_active_expert`:
   - Call `ExpertConsultationService(
     tenant=tenant, session_id=UUID(session_id), tool_registry=tools,
     session_store=session_store, sandbox_pool=sandbox_pool
     ).consult(expert=skill, task=args)`.
   - Build the expanded message as
     `[Expert {name} delivered:]\n{deliverable}\n\nUser request: {args}`.
   - Return it (plus a discriminator so the caller knows it was an expert
     invocation and emits the right event).
3. Otherwise: existing `skill_view`-based path (inline the SKILL.md body).

`expand_slash_skill`'s return signature gains an explicit `kind` field so
the caller can distinguish: today it returns
`tuple[str, str, str | None]` (`expanded_text`, `skill_name`,
`staged_at`); after the change it returns
`tuple[str, str, str | None, Literal["skill", "expert"]]`.

`surogates/harness/loop.py:1299-1328` (the eager-expansion call site) emits
`skill.invoked` today. For `kind == "expert"` it skips emitting
`skill.invoked` (the service has already emitted `expert.delegation`).

### Event flow after the change

```
User /sql_writer <task> ──┐
                          ├──> ExpertConsultationService.consult()
LLM consult_expert ───────┘     │
                                ├── emit expert.delegation
                                │     (before endpoint validation, so
                                │      missing-endpoint failures still
                                │      join in v_expert_outcomes)
                                ├── run_expert_loop
                                │     (tool calls emit tool.call / tool.result
                                │      via the parent session's tool router)
                                └── emit expert.result OR expert.failure
                                      │
                                      (no DB writes, no status flips,
                                       no expert_stats accumulation)
```

### `record_expert_outcome` simplification

`surogates/tools/builtin/expert_feedback.py` shrinks:

- Drop `AUTO_DISABLE_THRESHOLD`, `MIN_USES_FOR_AUTO_DISABLE` constants.
- Drop `_update_db_stats`.
- Drop `db_session` and `skill_id` parameters from
  `record_expert_outcome`.
- Function body keeps only the event emission (`EXPERT_RESULT` or
  `EXPERT_FAILURE` with the same payload shape).

The two callers in `expert_service.py:90, 178` drop the obsolete kwargs.

### Cleanups

Dead code excised (only kept because something used to call them):

| File | Symbol |
|---|---|
| `surogates/harness/expert_routing.py` | `select_expert_for_task`, `load_skills_for_expert_routing`, `classify_tool_calls`, `_TRIGGER_SPLIT_RE`, `_WORD_RE`, `_TRIGGER_STOPWORDS`, `_normalise_trigger_text`, `_trigger_match_score` |
| `surogates/harness/loop.py` | `_has_forced_expert_after_latest_user`, `_forced_expert_categories_after_latest_user`, `_legacy_forced_expert_categories_after_latest_user` |
| `surogates/tools/builtin/expert_feedback.py` | `_update_db_stats`, `AUTO_DISABLE_THRESHOLD`, `MIN_USES_FOR_AUTO_DISABLE`, the `db_session`/`skill_id` params on `record_expert_outcome` |
| `tests/test_expert_routing.py` (partial) | Tests covering the deleted helpers |

Do not delete the hard-task classifier, thinking helpers, or advisor support
that still live in `surogates/harness/expert_routing.py` and are imported by
`harness/loop.py` (`classify_hard_task_async`, `model_supports_thinking_toggle`,
`build_thinking_extra_body`, `merge_extra_body`).

Kept (unwired but harmless):

- `Skill.expert_stats` JSONB column (`surogates/db/models.py:702`) — never
  written; deferred to a follow-up migration if it bothers us.
- `v_expert_outcomes` SQL view — becomes populated naturally once
  `expert.delegation` and `expert.result` / `expert.failure` events flow.
- `get_active_experts` helper (`expert.py:68`) — used by the new prompt
  section.

### Documentation updates

`docs/experts/index.md` gets four edits:

1. **"What is an Expert?" paragraph.** Replace
   > "The harness automatically consults a matching active expert for hard
   > tasks before the default LLM answers or uses tools."

   with

   > "The base LLM consults an expert via `consult_expert` when a task
   > falls within its specialty. Users can also invoke an expert directly
   > with `/<expert-name> <task>`."

2. **Design Principle #1.** Replace "Hard tasks are expert-routed" with
   > "Experts are consulted voluntarily. The base LLM uses
   > `consult_expert` when a task falls within an active expert's
   > specialty; users can invoke experts directly via `/<expert>` slash
   > command."

3. **Auto-Disable subsection.** Delete entirely. Replace with a paragraph
   noting that every consultation emits `EXPERT_RESULT` or
   `EXPERT_FAILURE` for telemetry; `EXPERT_ENDORSE` / `EXPERT_OVERRIDE`
   are emitted only when a user or judge calls the feedback API on the
   resulting event. Together these drive training-data quality signals
   via `v_expert_outcomes`. Operators retire experts manually via
   `POST /v1/skills/{name}/retire`.

4. **Add "Slash invocation" subsection** under "Verify It Works":
   ```
   ### Slash invocation

   Users can also consult an expert directly:

   ```
   User: /sql_writer write me a query for the orders table

   Expert sql_writer (mini-loop):
     -> terminal: psql -c "\d orders"
     -> returns: "SELECT ... FROM orders ..."

   Base LLM (reviews and relays):
     Here's the query the sql_writer expert produced: ...
   ```
   ```

The lifecycle table (`draft → collecting → active → retired`) stays
accurate — operators still flip status manually through the existing
endpoints.

### API contract

No changes. Same `SKILL.md` schema, same `/v1/skills` endpoints, same
event types.

## Testing strategy

- Unit tests for the slash-command expert branch in `expand_slash_skill`.
- Unit test confirming `consult_expert` is registered after
  `ToolRuntime.register_builtins()` returns.
- Unit test confirming `TOOL_LOCATIONS["consult_expert"]` is
  `ToolLocation.HARNESS`.
- Unit test confirming the local `skills_list` handler includes expert
  metadata needed to identify active experts.
- Integration test: load a skill with `type: expert, expert_status: active`,
  send a chat completion request, verify the system prompt contains the
  `# Available Experts` section with that expert listed.
- Integration test: the LLM tool-call path emits `expert.delegation` and
  either `expert.result` or `expert.failure` for a contrived expert
  endpoint.
- Integration test: the slash path produces the synthetic user message
  with the expert's deliverable inlined and does not emit `skill.invoked`.
- Regression test: `delegate_task` is unaffected by the prompt section
  and tool description changes.
- Update the existing tests that currently assert `consult_expert` is hidden
  from `ToolRuntime`, `TOOL_LOCATIONS`, and executor prompt guidance.

## Open questions

None at this time. All design forks were resolved during brainstorming.

## Out of scope (follow-ups)

- Vault-backed expert credentials (parallel with MCP server credentials).
- Sub-sandbox isolation for the mini-loop.
- Dropping the unused `expert_stats` column via Alembic migration.
- Rate limiting / per-tenant concurrency on expert consultations.
- An evaluation harness to compute quality signals (replacing the
  removed auto-disable heuristic with explicit eval gates).
