# Agent Family Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, natively in one session; one fresh reviewer checks the whole branch at the end (Flavius's standing rule: no subagent-driven development). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `agent` tool family what the old delegation mechanisms have and it lacks: a per-call tool list, dependencies, queueing over the background limit, retries, recorded results, waiting for an answer, re-opening on resume, and stop/list for agents that have not started.

**Architecture:** A background agent that needs any of these gets a *record*: a row in the existing `tasks` table with a new `agent_dispatch` column set. The task dispatcher tick (`surogates/tasks/dispatcher.py`), which every worker replica runs every 5 s, starts, settles, retries and recovers records, starting them through the family's own start path (`delegate._run_single_delegation`, channel `delegation`). The tick alone decides a record's status and sends the notices that depend on it (a final failure, a stop); the end of a session only reports what it alone knows (its report, its recorded result, its question). A plain `agent` call gets no record and behaves as today. Everything stays behind `agents.subagent_parity`, which is off on PROD.

**Tech Stack:** Python 3.12, async SQLAlchemy on Postgres, Redis, pytest with testcontainers for integration tests.

**Spec:** `docs/superpowers/specs/2026-11-12-agent-family-only-design.md`

**Where this sits:** the spec is delivered by three plans. This is the first. The second moves the consumers onto the family with the flag on (missions, deep research, project threads, Keel, governance). The third removes the flag and the old tools and changes the other repositories. Each later plan is written when the one before it has landed. Carry these into the second plan: a thread's records need the hand-on of Task 3 to be tested end to end with a real thread; and mission agents' own foreground sub-agents need records too, since "agents those agents start" are linked to the mission.

## Global Constraints

- With `agents.subagent_parity` off, nothing changes: the tools offered and every existing test stay as they are. Every new clause in shared code (the tick, the claim, `tell_parent`, the task-worker checks) applies only to rows with `agent_dispatch IS NOT NULL` or to sessions with `agent_dispatch` in their config. Old `spawn_task` rows are claimed, settled and retried exactly as today.
- A plain `agent` call (no `after`, no `retries`, under the limit) creates no record and returns the child's session id as `agent_id`, as today.
- An agent has one id for its whole life: its record's id when it has a record, else its session's id. A record made for an agent that already runs (adopted) takes the session's id as its own.
- `MAX_RUNNING_IN_BACKGROUND` stays 5 and is read from `agents.MAX_RUNNING_IN_BACKGROUND` wherever it is checked, so tests can patch it in one place. The depth limit stays 2. `retries` is 0 to 5, default 0.
- Reports and notification texts are cut to `MAX_RESULT_CHARS` (10,000). A recorded result is at most 20,000 characters of JSON. JSON shown to the model is `json.dumps(value, sort_keys=True)`: Postgres reorders the keys of stored JSON.
- A new builtin tool needs an entry in `TOOL_LOCATIONS` (`surogates/tools/router.py`), or it answers "Unknown tool".
- Schema changes go in `surogates/db/models.py` and, as idempotent SQL, in `surogates/db/observability.sql`. There is no Alembic here.
- Never use `SELECT ... FOR UPDATE` on a `tasks` row that a child session's insert will reference: it deadlocks with the insert's key-share lock (see `surogates/tasks/service.py`). Claim with `UPDATE ... RETURNING`.
- Read the agent and org from the session, never from `SUROGATES_AGENT_ID` / `SUROGATES_ORG_ID`.
- Tool descriptions stay general: no product names, no file names.
- No plan or task numbers in code comments or commit messages. Conventional commits. No `Co-Authored-By` trailer.
- Work in a git worktree outside `/work` and outside `/tmp` (`git -C /work/surogates worktree add -b feat/agent-family-capabilities ~/worktrees/surogates-agent-family origin/master`). Never check out or pull in `/work/surogates`.
- In the worktree, make its own venv once: `uv sync --frozen --python 3.12 --all-extras`. Run only the affected test files, one worker: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest <files> -n 0 -p no:cacheprovider -q`. The integration database is shared by every test of a run: assert on your own chat's rows, never on counts across the database.
- Keep `docs/sub-agents/index.md` section 13 in step: each task that adds an argument, a tool or a status adds it to that section.

## Review Focus

1. A worker dies after it created an agent's session and before it wrote `current_session_id`. Expected: a later tick wires the session it finds, and no second session is started (test in Task 4).
2. Two ticks, on two workers, run at the same moment. Expected: every ready record starts once, and a failed attempt is settled and retried once (test in Task 4).
3. The parent is stopped by its user, failed or deleted while its agents wait or queue. Expected: none of them starts; they start once the parent is resumed (test in Task 6).
4. An old `spawn_task` row and a record are both ready in the same tick. Expected: each starts by its own path; the record's child is offered no `worker_*` tool, the old row's child still is (test in Task 4).
5. A recorded result that is not an object or is over 20,000 characters, and a dependency's report over 10,000 characters. Expected: the first is refused with a clear error, the second is cut, and neither breaks a start (tests in Tasks 5 and 8).

---

## File Structure

| File | Responsibility |
|---|---|
| `surogates/harness/agent_records.py` (new) | The record behind an agent: create, adopt, resolve an id, build the brief, classify a run, settle, announce a final end, re-open, cancel, list. |
| `surogates/tools/builtin/agents.py` | The tools: new arguments of `agent` and `send_message`, the `record_result` tool, `start_record`, stop/list/message with records. |
| `surogates/tools/builtin/delegate.py` | `child_tool_policy(call_tools=)`, `_run_single_delegation(task_id=)`, the foreground result's notes. |
| `surogates/harness/subagents.py` | What is offered to whom, `tell_parent` aware of records, a run's recorded result and its question. |
| `surogates/harness/agent_news.py` | How the new notification fields read to the model. |
| `surogates/tasks/dispatcher.py` | The tick: recover, re-open, settle and start records; old rows untouched. |
| `surogates/tasks/tools.py` | The old task tools refuse records. |
| `surogates/db/models.py`, `surogates/db/observability.sql` | `tasks.agent_dispatch`, `tasks.claimed_at`. |
| `surogates/session/events.py` | `AGENT_RESULT`, `AGENT_WAITING`. |
| `tests/integration/test_agent_records.py` (new) | Records, the start, the tick, the queue, and the helpers the other new files import: `tick`, `record_of`, `a_record`, `a_started`, `several_calls`, `fail`. |
| `tests/integration/test_agent_dependencies.py` (new) | `after`. |
| `tests/integration/test_agent_retries.py` (new) | `retries` and an agent that runs again. |
| `tests/integration/test_agent_results.py` (new) | `record_result` and waiting for an answer. |

Record statuses are the `tasks` statuses. The model sees them through one map, defined once in `agents.py`:

```python
RECORD_STATUS = {
    "todo": "waiting_for_agents", "ready": "queued", "running": "running", "blocked": "waiting_for_answer",
    "done": "completed", "failed": "failed", "cancelled": "stopped",
}
```

The brief an attempt starts from, built by `brief_for`; each section appears only when it has content, and with neither the brief is the prompt alone:

```text
## Reports of the agents this one waited for

### <description> (<agent id>)
<report, cut to 10,000 characters>

## Earlier attempts

- Attempt 1 (failed): <its last words, cut to 500 characters, or "it ended without a report">

## Your task

<prompt>
```

New test files import the fixtures the way `tests/integration/test_send_message.py` does:

```python
from .test_agent_tool import PARITY, a_chat, calling, chats, children, tool_result  # noqa: F401  (chats is a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
```

---

### Task 1: A per-call tool list for `agent`

**Files:**
- Modify: `surogates/tools/builtin/delegate.py` (`child_tool_policy`, `_run_single_delegation`)
- Modify: `surogates/tools/builtin/agents.py` (`_AGENT_SCHEMA`, `_agent_handler`, `_start_agent`)
- Modify: `docs/sub-agents/index.md` (section 13, the `agent` row)
- Test: `tests/test_delegate.py`, `tests/integration/test_agent_tool.py`

**Interfaces:**
- Produces: `child_tool_policy(*, parent_config, agent_def, role, leaf_tools=("delegate_task",), call_tools: list[str] | None = None) -> tuple[list[str] | None, list[str]]`
- Produces: `_run_single_delegation` reads `task.get("tools")` and passes it as `call_tools`.
- Produces: `agent` argument `tools`: an array of at most 50 tool names.

Rules for `call_tools`, in order: it replaces the agent type's `tools`; it is intersected with the parent's `allowed_tools` when the parent has one; names in the agent type's `disallowed_tools`, in the parent's `excluded_tools` and in `_DELEGATION_ALWAYS_BLOCKED_TOOLS` are dropped; a leaf loses `leaf_tools` even when the call names them; order is kept and duplicates dropped.

- [ ] **Step 1: Write the failing tests**

In `tests/test_delegate.py`:

```python
def test_call_tools_win_over_the_agent_types_and_lose_what_may_not_run():
    allowed, _ = child_tool_policy(
        parent_config={"excluded_tools": ["terminal"]},
        agent_def=SimpleNamespace(tools=["read_file"], disallowed_tools=["write_file"]),
        role="orchestrator", leaf_tools=("delegate_task", "agent"),
        call_tools=["web_search", "terminal", "write_file", "ask_user_question", "agent", "web_search"],
    )
    assert allowed == ["web_search", "agent"]


def test_call_tools_never_reach_past_the_parents_allowlist():
    allowed, _ = child_tool_policy(
        parent_config={"allowed_tools": ["web_search", "read_file"]}, agent_def=None, role="orchestrator",
        leaf_tools=("delegate_task", "agent"), call_tools=["web_search", "terminal"],
    )
    assert allowed == ["web_search"]


def test_a_leaf_loses_agent_even_when_the_call_names_it():
    allowed, _ = child_tool_policy(
        parent_config={}, agent_def=None, role="leaf", leaf_tools=("delegate_task", "agent"),
        call_tools=["web_search", "agent"],
    )
    assert allowed == ["web_search"]
```

In `tests/integration/test_agent_tool.py`:

```python
async def test_an_agent_started_with_tools_gets_those_it_may_have(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Research the product", prompt="Research it.",
                run_in_background=True, tools=["web_search", "read_file", "agent", "ask_user_question"]),
        _final_response("Started."),
    ]
    await chats.says(chat, "Research the product.")
    await chats.wake(chat)
    [child] = await children(chats, chat)
    assert child.config["allowed_tools"] == ["web_search", "read_file", "agent"]
```

- [ ] **Step 2: Run them and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_delegate.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q -k "call_tools or leaf_loses_agent or started_with_tools"`
Expected: 4 failed (`child_tool_policy() got an unexpected keyword argument 'call_tools'`, and `allowed_tools` missing).

- [ ] **Step 3: Implement**

Add `call_tools` to `child_tool_policy` with the rules above. In `_run_single_delegation` pass `call_tools=task.get("tools")`. In `agents.py` add the property

```python
"tools": {
    "type": "array", "items": {"type": "string"}, "maxItems": 50,
    "description": "Optional list of tool names the agent may use; omitted, it gets the tools of its agent_type, or yours.",
},
```

read it in `_agent_handler` (a list of non-empty strings, else the error `tools must be a list of tool names`), and hand it to `_start_agent`, which puts it in the `task` dict as `"tools"`.

- [ ] **Step 4: Run the two test files whole and see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_delegate.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add `tools?` to the `agent` row of section 13 in `docs/sub-agents/index.md`, then commit**

```bash
git add surogates/tools/builtin/delegate.py surogates/tools/builtin/agents.py tests/test_delegate.py tests/integration/test_agent_tool.py docs/sub-agents/index.md
git commit -m "feat(agents): the agent tool takes a list of tools for the sub-agent"
```

---

### Task 2: The record behind an agent

**Files:**
- Create: `surogates/harness/agent_records.py`
- Modify: `surogates/db/models.py` (`Task`), `surogates/db/observability.sql`
- Modify: `surogates/tools/builtin/delegate.py` (`_run_single_delegation`)
- Modify: `surogates/harness/loop.py` (the `session.task_id is not None` checks at the question timeout and in `_apply_execution_context_gates`), `surogates/orchestrator/worker.py` (`_filter_effective_tools`)
- Test: `tests/integration/test_agent_records.py`

**Interfaces:**
- Produces, in `surogates/db/models.py`: `Task.agent_dispatch: Mapped[Optional[dict[str, Any]]]` (JSONB, nullable; its keys `description`, `model`, `tools`, `tool_call_id`, `tokens`, and later `last_error`; null for an old task-layer row) and `Task.claimed_at: Mapped[Optional[datetime]]` (when the current attempt was claimed; set on every claim).
- Produces, in `surogates/db/observability.sql`:

```sql
ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS agent_dispatch jsonb;
ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
```

- Produces, in `agent_records.py`:

```python
RECORD_KEY = "record"   # in a sub-agent's agent_dispatch: the id of its tasks row

class AgentRef(NamedTuple):
    record_id: UUID | None           # its tasks row, if it has one
    session_id: UUID | None          # its current session; None while it has not started
    parent_session_id: UUID | None   # the session that started it; None for a session nobody started
    status: str                      # a tasks status for a record, else the session's status

def agent_id_of(session: Any) -> str: ...        # its record's id when its config's dispatch names one, else the session's
def is_task_worker(session: Any) -> bool: ...    # task_id set and no agent_dispatch in its config; uses getattr
async def create_record(session_factory, *, parent: Any, prompt: str, description: str, agent_type: str | None,
                        model: str | None, tools: list[str] | None, tool_call_id: str | None,
                        after: list[UUID], retries: int, tokens: int, start_now: bool) -> Task: ...
async def resolve(session_factory, raw: str) -> AgentRef | None: ...   # raw is a record id or a session id
async def release(session_factory, record_id: UUID) -> None: ...       # running -> ready, the attempt given back
```

- Produces: `_run_single_delegation(..., task_id: UUID | None = None)`, passed to `create_child_session(task_id=task_id)`.

`create_record` writes `goal=prompt`, `agent_def_name=agent_type`, `max_attempts=retries + 1`, `parent_session_id=parent.id`, `org_id=parent.org_id`, `context=None`, `mission_id=None`, and one `TaskLink` per id in `after`. With `start_now=True` it inserts the row `running` with `attempt_count=1`, `started_at` and `claimed_at` now, so no tick can claim it before the caller starts it. Otherwise it inserts `todo` when any record in `after` is not `done`, else `ready`, with `attempt_count=0` set explicitly (server defaults are not loaded back after an insert). Before the insert, after its own checks, it calls `surogates.session.provisioning.before_child(db)`, as `create_task_and_spawn` does: a project thread's step hands its copy on there, so an agent the tick starts later works on the thread's files as they were at the call. `NotHandedOn` propagates.

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_agent_records.py` starts with the shared helpers:

```python
async def tick(chats) -> None:
    """Dispatcher ticks over the test database until one moves nothing: the database is shared by the
    session's tests, so a test asserts on its own chat's agents, never on a tick's counts."""
    state = chats.api.app.state
    for _ in range(10):
        counts = await tasks_tick(
            session_factory=state.session_factory, redis=state.redis, session_store=chats.store,
            tenant_for_task=lambda task: SimpleNamespace(
                org_id=task.org_id, user_id=chats.api.user_id, service_account_id=None, asset_root="/tmp/test",
            ),
        )
        if not any(counts.get(key) for key in ("recovered", "reopened", "settled", "promoted", "finalized", "enqueued")):
            return


async def record_of(chats, ref) -> Task | None:
    """The record with id *ref*, or whose current session is *ref* (an id or a session); None when there is none."""
    key = ref if isinstance(ref, UUID) else ref.id
    async with chats.api.app.state.session_factory() as db:
        return (await db.execute(
            select(Task).where(or_(Task.id == key, Task.current_session_id == key)),
        )).scalars().first()


async def a_record(chats, chat: UUID, **over) -> Task:
    parent = await chats.session(chat)
    return await create_record(chats.api.app.state.session_factory, **{
        "parent": parent, "prompt": "Check the totals.", "description": "Check the totals", "agent_type": None,
        "model": None, "tools": None, "tool_call_id": "call_agent", "after": [], "retries": 0, "tokens": 0,
        "start_now": False, **over,
    })


async def a_started(chats, chat: UUID, call_id: str, **arguments) -> dict:
    """The chat starts an agent in the background with *arguments*; the call's result."""
    chats.replies = [calling("agent", call_id, run_in_background=True, **arguments), _final_response("Started.")]
    await chats.says(chat, "Start it.")
    await chats.wake(chat)
    return json.loads(await tool_result(chats, chat, call_id))
```

and the tests:

```python
async def test_a_record_is_ready_at_once_or_waits_for_the_records_it_names(chats):
    chat = await a_chat(chats, PARITY)
    first = await a_record(chats, chat, retries=2)
    assert (first.status, first.max_attempts, first.attempt_count, first.mission_id) == ("ready", 3, 0, None)
    assert first.agent_dispatch == {
        "description": "Check the totals", "model": None, "tools": None, "tool_call_id": "call_agent", "tokens": 0,
    }
    second = await a_record(chats, chat, after=[first.id])
    assert second.status == "todo"
    started = await a_record(chats, chat, start_now=True)
    assert (started.status, started.attempt_count) == ("running", 1) and started.claimed_at is not None


async def test_an_id_resolves_to_a_record_or_a_session_and_nothing_else(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    factory = chats.api.app.state.session_factory
    assert await resolve(factory, str(record.id)) == AgentRef(record.id, None, chat, "ready")
    assert (await resolve(factory, str(chat))).session_id == chat
    assert await resolve(factory, "not-an-id") is None and await resolve(factory, str(uuid4())) is None


async def test_a_record_released_goes_back_to_ready_with_its_attempt_given_back(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat, start_now=True)
    await release(chats.api.app.state.session_factory, record.id)
    again = await record_of(chats, record.id)
    assert (again.status, again.attempt_count) == ("ready", 0)


async def test_a_sub_agent_with_a_record_is_no_task_worker():
    plain = SimpleNamespace(task_id=uuid4(), config={})
    family = SimpleNamespace(task_id=uuid4(), config={DISPATCH_KEY: {RECORD_KEY: "x"}})
    assert is_task_worker(plain) and not is_task_worker(family)
    assert not is_task_worker(SimpleNamespace(task_id=None, config={})) and not is_task_worker(SimpleNamespace())
```

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py -n 0 -p no:cacheprovider -q`
Expected: collection error, `No module named 'surogates.harness.agent_records'`.

- [ ] **Step 3: Implement**

Add the two columns and the SQL lines, write `agent_records.py` with the signatures above, add `task_id` to `_run_single_delegation`, and replace the `task_id is not None` checks named under Files with `is_task_worker(session)`.

- [ ] **Step 4: Run the new file and the files of the checks you changed**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/tasks tests/test_case_tool_gate.py tests/test_prompt_honours_excluded_tools.py tests/test_unattended_sessions.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/agent_records.py surogates/db/models.py surogates/db/observability.sql surogates/tools/builtin/delegate.py surogates/harness/loop.py surogates/orchestrator/worker.py tests/integration/test_agent_records.py
git commit -m "feat(agents): a record in the tasks table for an agent that waits, queues or retries"
```

---

### Task 3: Start an agent through its record

**Files:**
- Modify: `surogates/tools/builtin/agents.py` (`_AGENT_SCHEMA`, `_agent_handler`, new `start_record`, `_send_message_handler`, `_stop_agent_handler`)
- Modify: `surogates/harness/agent_records.py` (`brief_for`)
- Modify: `surogates/harness/subagents.py` (`tell_parent`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_records.py`

**Interfaces:**
- Consumes: `create_record`, `release`, `resolve`, `agent_id_of`, `RECORD_KEY` (Task 2).
- Produces, in `agents.py`:

```python
class RecordNotStartable(ValueError): ...   # this record can never start: unknown agent_type, no iterations, no workspace

async def start_record(record: Task, *, session_store, session_factory, redis, tenant, bundle=None,
                       file_bundle_cache=None, memory_manager=None) -> Any:
    """Start the attempt *record* was claimed for, as a background agent of its parent; returns its session.
    Raises RecordNotStartable for what can never succeed, and any other exception for what may next time."""
```

- Produces, in `agent_records.py`: `async def brief_for(session_factory, session_store, record: Task) -> str` (the prompt alone in this task; Tasks 5 and 7 add its sections, in the format under File Structure).
- Produces: `agent` argument `retries` (integer 0 to 5). A call with `retries > 0` goes through a record.
- Produces: `send_message` and `stop_agent` accept a record's id for an agent that has started, through `resolve`. Every id they print (the message's header, the reply's `to`, the `stopped` list) is `agent_id_of` the agent.

Behaviour:
- `retries > 0` needs `run_in_background=true` (error: `after and retries need run_in_background=true: a call that waits for its report cannot wait for other agents or start again`) and no `fork` (error: `a fork cannot use after or retries: start it with a brief`).
- Before it creates a record, the handler resolves `agent_type`, as `_run_single_delegation` would, and refuses an unknown one with the same error (`Unknown or disabled agent_type: …`): a record is never made for what can never start.
- Under the parent's `_start_lock`, the handler creates the record with `start_now=True` and calls `start_record`. It returns `{"agent_id": <record id>, "status": "running", "description": ..., "note": STARTED_NOTE}`. On `RecordNotStartable` it sets the record `failed` (`completed_at` now) and returns the error. On any other exception it calls `release`, so the tick starts it, and returns the record's id with `"status": "queued"` and `QUEUED_NOTE` (Task 6 defines the note; until then use `"It starts in a moment."`).
- `start_record`:
  - resolves `agent_type` (`RecordNotStartable` when unknown or disabled) and the bundle (`bundle`, else `file_bundle_cache.get(parent.agent_id)`, as `surogates/tasks/spawn.py` does);
  - computes the iteration cap as `_start_agent` does, with no turn budget (`delegate._CHILD_MAX_ITERATIONS`); `RecordNotStartable` when it is 0 or less;
  - calls `delegate._run_single_delegation` with `task_id=record.id`, the brief from `brief_for`, `agent_type=record.agent_def_name`, the dispatch's `model` and `tools`, depth and role from the parent's `delegation_depth` as `_agent_handler` computes them, `extra_config` as `_start_agent` builds it with mode `background` and `RECORD_KEY: str(record.id)` in the dispatch, `start_extra` as `_start_agent` builds it (`agent: True`, `description`, `mode`, `fork: False`, `tool_call_id`), and an `on_started` that keeps the child and ends the call;
  - when `on_started` never fired, the call started no child: a returned error naming the agent type, the iteration budget or a missing workspace raises `RecordNotStartable` with that text; any other raises `RuntimeError` with it;
  - writes `tasks.current_session_id` to the child's id and returns the child.
- `tell_parent` re-reads the session (`store.get_session(session.id)`) and names the agent `agent_id_of` it. For a session with a record, `failed` and `killed` write nothing to the parent: the tick decides whether the agent starts again and tells the parent then (Task 4). When `error` is given, it is stored first with `note_failure` (Task 4). `completed` and `budget` are told at once, as today.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_with_retries_is_started_through_a_record_and_named_by_it(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_agent", description="Check the totals", prompt="Check the totals.", retries=2)
    [child] = await children(chats, chat)
    record = await record_of(chats, child)
    assert started["agent_id"] == str(record.id) != str(child.id) and started["status"] == "running"
    assert (record.status, record.attempt_count, record.max_attempts, record.current_session_id) == ("running", 1, 3, child.id)
    assert child.channel == "delegation" and child.task_id == record.id
    assert child.config[DISPATCH_KEY][RECORD_KEY] == str(record.id)
    [start] = [e for e in await chats.store.get_events(chat, types=[EventType.DELEGATION_START])
               if e.data["child_session_id"] == str(child.id)]
    assert (start.data["agent"], start.data["mode"], start.data["tool_call_id"]) == (True, "background", "call_agent")

    chats.replies = [_final_response("The totals match.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert (note.data["agent_id"], note.data["status"]) == (str(record.id), "completed")


@pytest.mark.parametrize("extra, error", [
    ({"retries": 1}, "after and retries need run_in_background=true"),
    ({"retries": 1, "run_in_background": True, "fork": True}, "a fork cannot use after or retries"),
    ({"retries": 1, "run_in_background": True, "agent_type": "no-such-type"}, "Unknown or disabled agent_type"),
])
async def test_a_call_that_cannot_start_makes_no_record(chats, extra, error):
    chat = await a_chat(chats, PARITY)
    chats.replies = [calling("agent", "call_agent", description="x", prompt="Do x.", **extra), _final_response("No.")]
    await chats.says(chat, "Do x.")
    await chats.wake(chat)
    assert error in json.loads(await tool_result(chats, chat, "call_agent"))["error"]
    assert await children(chats, chat) == []
    async with chats.api.app.state.session_factory() as db:
        assert (await db.execute(select(Task).where(Task.parent_session_id == chat))).scalars().all() == []


async def test_a_message_and_a_stop_reach_an_agent_by_its_records_id(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_agent", description="Check", prompt="Check.", retries=1)
    chats.replies = [
        calling("send_message", "call_message", to=started["agent_id"], message="Also the fees.", summary="Fees"),
        calling("stop_agent", "call_stop", agent_id=started["agent_id"]),
        _final_response("Done."),
    ]
    await chats.says(chat, "Add the fees, then stop it.")
    await chats.wake(chat)
    sent = json.loads(await tool_result(chats, chat, "call_message"))
    stopped = json.loads(await tool_result(chats, chat, "call_stop"))
    assert (sent["status"], sent["to"]) == ("delivered", started["agent_id"])
    assert (stopped["status"], stopped["stopped"]) == ("stopped", [started["agent_id"]])


async def test_a_failure_of_a_record_is_not_told_by_the_sessions_end(chats):
    chat = await a_chat(chats, PARITY)
    await a_started(chats, chat, "call_agent", description="Check", prompt="Check.", retries=1)
    [child] = await children(chats, chat)
    await fail(chats, child, "provider error")
    assert await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION]) == []
    assert (await record_of(chats, child)).agent_dispatch["last_error"] == "provider error"
```

`fail` goes in this file:

```python
async def fail(chats, session, error: str) -> None:
    """End a sub-agent as a failed turn does: its status first, then its parent told."""
    await chats.store.update_session_status(session.id, "failed")
    await tell_parent(store=chats.store, redis=chats.api.app.state.redis, session=await chats.session(session.id),
                      status="failed", error=error)
```

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py -n 0 -p no:cacheprovider -q`
Expected: the six new tests fail (`retries` is not an argument; `note_failure` is not defined).

- [ ] **Step 3: Implement as described under Interfaces and Behaviour**

`note_failure(session_factory, session_id, error)` lives in `agent_records.py` (Task 4 reads it): it sets `agent_dispatch = agent_dispatch || {"last_error": error[:2000]}` on the record whose `current_session_id` is the session. `tell_parent` reaches the database through `store._sf`. The `retries` property:

```python
"retries": {
    "type": "integer", "minimum": 0, "maximum": 5,
    "description": "Optional number of times to start it again from its brief if it fails (default 0). Needs run_in_background=true.",
},
```

- [ ] **Step 4: Run the family's test files and the task layer's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/test_agent_tool.py tests/integration/test_send_message.py tests/integration/test_list_stop_agents.py tests/integration/tasks -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add `retries?` to section 13, and a paragraph saying an agent with a record is named by one id across its attempts; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/harness/agent_records.py surogates/harness/subagents.py tests/integration/test_agent_records.py docs/sub-agents/index.md
git commit -m "feat(agents): an agent with retries starts through its record"
```

---

### Task 4: The tick runs records

**Files:**
- Modify: `surogates/tasks/dispatcher.py` (`_finalize_ended_sessions`, `_enqueue_ready_tasks`, new `_recover_unstarted_records`, `_start_ready_records`, `tasks_tick`)
- Modify: `surogates/harness/agent_records.py` (`RunOutcome`, `run_of`, `classify_run`, `settle`, `announce_end`, `waiting_on`)
- Modify: `surogates/harness/agent_news.py` (`notification_note`)
- Modify: `surogates/tasks/tools.py` (`unblock_task`, `cancel_task`, `spawn_task`'s `parents`)
- Test: `tests/integration/test_agent_records.py`, `tests/integration/tasks/test_spawn_task_tool.py`

**Interfaces:**
- Produces, in `agent_records.py`:

```python
class RunOutcome(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"      # used from Task 9 on
    FAILED = "failed"
    STOPPED = "stopped"

def run_of(events: list[Any]) -> list[Any]: ...      # the events of the session's last run
def classify_run(events: list[Any], session_status: str) -> RunOutcome: ...
async def settle(session_factory, session_store, redis, record_ids: list[UUID] | None = None) -> int: ...
async def announce_end(session_store, redis, record: Task, status: str) -> None: ...   # failed or killed, once
async def waiting_on(session_factory, record_id: UUID) -> list[UUID]: ...   # the records still todo or ready that name this one
async def note_failure(session_factory, session_id: UUID, error: str) -> None: ...      # (Task 3)
```

- Produces: `tasks_tick` returns `recovered` and `settled` besides its existing counts.

Behaviour:
- **Claims of records** (`_start_ready_records`, called by `tasks_tick` after the old `_enqueue_ready_tasks`): one record at a time, in its own transaction: take `pg_advisory_xact_lock(hashtextextended(parent_session_id::text, 0))` for the parent of the oldest claimable record, check it is still claimable, and `UPDATE tasks SET status='running', attempt_count=attempt_count+1, current_session_id=NULL, claimed_at=now(), started_at=coalesce(started_at, now()) WHERE id=:id AND status='ready' RETURNING *`. Claimable means `agent_dispatch IS NOT NULL`, `status='ready'`, and no `TaskLink` from it points at a record that is not `done` (Task 6 adds the parent and slot conditions). Then, outside the transaction, `start_record`. `RecordNotStartable` sets the record `failed` and calls `announce_end(..., "failed")` with the error in `last_error`. Any other exception keeps the attempt it used, as `_retry_or_block` does: the record goes back to `ready` while `attempt_count < max_attempts`, else `failed` with `announce_end`. Clearing `current_session_id` on the claim is what keeps another replica's settle from joining the claimed record to its failed attempt; earlier attempts are found by `sessions.task_id`.
- **Old rows**: `_enqueue_ready_tasks` and `_finalize_ended_sessions` gain `Task.agent_dispatch.is_(None)` in their selects and nothing else.
- **Recovery** (`_recover_unstarted_records`, first in `tasks_tick`): a record `running` with `current_session_id IS NULL` and `claimed_at` more than 60 s ago: if a session with `task_id` = the record and `created_at >= claimed_at` exists, wire the newest one as `current_session_id`; else go back to `ready` with the attempt given back. Filter on `agent_dispatch IS NOT NULL`.
- **`settle`** (in `tasks_tick` as `settled`, and callable for given ids): for each record `running` whose `current_session_id` session is `completed`, `failed`, `cancelled` or `paused`, by `classify_run` over `run_of` its events, in one `UPDATE ... WHERE status='running' AND current_session_id=:session` (a settle racing another settles once):
  - `COMPLETED`: `done`, `result` the run's final message (the `budget_report` for a budget stop), `completed_at` now. Nothing is announced: the session's end told the parent.
  - `FAILED`: `ready` while `attempt_count < max_attempts`; else `failed`, `completed_at` now, and `announce_end(..., "failed")`.
  - `STOPPED`: `cancelled`, `completed_at` now, `announce_end(..., "killed")`.
- `classify_run`: `completed` gives `COMPLETED`; `failed` or `cancelled` gives `FAILED`; `paused` gives `STOPPED`. `run_of` is the slice `loop_artifact_completion.py` takes for a resumed agent: the events after the `session.complete` before the last one.
- **`announce_end`** writes what `tell_parent` writes for a background agent's `failed` or `killed` end (`delegation.failed` for a failure, then `agent.notification`), with `agent_id` the record's id, `summary` the description, `result` the last run's final message, `error` from `last_error` (else `unknown error`), `tool_call_id` from the dispatch, `attempts` when `attempt_count` is over 1, and `waiting_on_it` (from `waiting_on`) when it is not empty.
- `notification_note`, for an agent's `failed` with `attempts`, says `failed after <n> attempts: <error>`; with `waiting_on_it`, it adds a last line `[1 agent waits for it and will not start: stop it or start a replacement.]` (`<n> agents wait` for more).
- **Old tools**: `unblock_task` and `cancel_task` given a record's id, and `spawn_task` given one in `parents`, answer `<id> is an agent of the agent tool: use send_message or stop_agent` and change nothing.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_tick_starts_a_ready_record_as_a_sub_agent_of_the_family(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    await tick(chats)
    [child] = await children(chats, chat)
    assert child.channel == "delegation" and child.task_id == record.id
    chats.replies = [_final_response("Done.")]
    await chats.wake(child.id)
    assert not WORKER_SELF_TOOLS & set(chats.offered[-1])
    await tick(chats)
    done = await record_of(chats, record.id)
    assert (done.status, done.result) == ("done", "Done.")


async def test_a_record_whose_start_was_cut_short_is_started_once(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat, start_now=True)       # claimed by a worker that died before its child
    await set_claimed_at(chats, record.id, seconds_ago=61)
    await tick(chats)
    [child] = await children(chats, chat)
    recovered = await record_of(chats, record.id)
    assert (recovered.status, recovered.attempt_count, recovered.current_session_id) == ("running", 1, child.id)


async def test_a_session_started_before_the_worker_died_is_wired_not_started_again(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_agent", description="Check", prompt="Check.", retries=1)
    [child] = await children(chats, chat)
    await clear_current_session(chats, UUID(started["agent_id"]))   # the wiring never happened
    await set_claimed_at(chats, UUID(started["agent_id"]), seconds_ago=61)
    await tick(chats)
    assert await children(chats, chat) == [child]
    assert (await record_of(chats, UUID(started["agent_id"]))).current_session_id == child.id


async def test_two_ticks_at_once_start_each_record_and_retry_each_failure_once(chats):
    chat = await a_chat(chats, PARITY)
    records = [await a_record(chats, chat, retries=1) for _ in range(3)]
    await asyncio.gather(tick(chats), tick(chats))
    assert len(await children(chats, chat)) == 3
    first = next(c for c in await children(chats, chat) if c.task_id == records[0].id)
    await fail(chats, first, "provider error")
    await asyncio.gather(tick(chats), tick(chats))
    retried = await record_of(chats, records[0].id)
    assert retried.attempt_count == 2 and len(await children(chats, chat)) == 4


async def test_a_record_that_fails_for_good_is_told_once_by_the_tick(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_agent", description="Check", prompt="Check.", retries=1)
    [first] = await children(chats, chat)
    await fail(chats, first, "provider error")
    await tick(chats)
    second = next(c for c in await children(chats, chat) if c.id != first.id)
    await fail(chats, second, "provider error again")
    await tick(chats)
    await tick(chats)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert (note.data["agent_id"], note.data["status"], note.data["attempts"]) == (started["agent_id"], "failed", 2)
    assert "failed after 2 attempts: provider error again" in notification_note(note.data)["content"]
    assert (await record_of(chats, UUID(started["agent_id"]))).status == "failed"


async def test_an_old_task_and_a_record_in_one_tick_each_start_by_their_own_path(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    async with chats.api.app.state.session_factory() as db:
        old = Task(org_id=(await chats.session(chat)).org_id, parent_session_id=chat, goal="Old work.", status="ready")
        db.add(old)
        await db.commit()
    await tick(chats)
    by_task = {child.task_id: child for child in await children(chats, chat)}
    assert by_task[record.id].channel == "delegation" and by_task[old.id].channel == "task"
```

`set_claimed_at(chats, record_id, seconds_ago)` and `clear_current_session(chats, record_id)` are two-line helpers in this file that update the row. In `tests/integration/tasks/test_spawn_task_tool.py` add one test that `unblock_task`, `cancel_task` and `spawn_task(parents=[record id])` each answer the "is an agent of the agent tool" error and leave the record as it was.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/tasks/test_spawn_task_tool.py -n 0 -p no:cacheprovider -q`
Expected: the seven new tests fail (the tick starts the record on channel `task`; no recovery; no `attempts` in a notice).

- [ ] **Step 3: Implement as described under Behaviour**

- [ ] **Step 4: Run the records', the family's and the task layer's files**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/test_agent_tool.py tests/integration/test_list_stop_agents.py tests/integration/tasks tests/integration/missions -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/tasks/dispatcher.py surogates/harness/agent_records.py surogates/harness/agent_news.py surogates/tasks/tools.py tests/integration/test_agent_records.py tests/integration/tasks/test_spawn_task_tool.py
git commit -m "feat(agents): the dispatcher tick starts, settles, retries and recovers agents' records"
```

---

### Task 5: Dependencies

**Files:**
- Modify: `surogates/tools/builtin/agents.py` (`_AGENT_SCHEMA`, `_agent_handler`)
- Modify: `surogates/harness/agent_records.py` (`adopt`, `brief_for`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_dependencies.py`

**Interfaces:**
- Consumes: `settle`, `waiting_on` (Task 4).
- Produces, in `agent_records.py`: `async def adopt(session_factory, session: Any) -> UUID` (a record for a background agent that has none, with `id` = the session's id).
- Produces: `agent` argument `after`: an array of at most 10 agent ids.

Behaviour:
- `after` takes the same two refusals as `retries` (Task 3).
- Under the parent's `_start_lock`, the handler first settles the named records (`settle(record_ids=...)`), so a dependency whose session already ended is judged by its outcome. Each id must then resolve to an agent the caller started in the background (`parent_session_id` is the caller, mode `background`): otherwise `no agent <id> that you started in the background`. A named agent that failed or was stopped (its record's status, or its session's when it has no record) is refused with `agent <id> failed: it cannot be waited for` (or `was stopped`). All ids are checked before anything is written: nothing is created or adopted on a refusal.
- A named background agent without a record is adopted: `adopt` inserts a record with `id` = the session's id, `INSERT ... ON CONFLICT (id) DO NOTHING` (two calls in one response adopt it once), `current_session_id` the session, `attempt_count` and `max_attempts` 1, `claimed_at` now, and the status its session gives (`running`; `done` with its final message as `result`; `failed`; `cancelled` for a paused one), then sets the session's `task_id` and writes `RECORD_KEY` into its dispatch. Its id does not change.
- The call returns `{"agent_id": <record id>, "status": "waiting_for_agents", "waiting_for": [...], "description": ..., "note": WAITING_NOTE}` when it waits, and starts at once (as in Task 3, `start_now=True`) when every dependency is already `done`.
- `WAITING_NOTE = "It starts once every agent it waits for has completed. If one of them fails or is stopped, it stays waiting and you are told: stop it or start a replacement."`
- `brief_for` adds the "Reports of the agents this one waited for" section: each dependency's `result`, cut to `MAX_RESULT_CHARS`, under `### <description> (<agent id>)`, in the order of `after`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_waits_for_another_and_starts_with_its_report(chats):
    chat = await a_chat(chats, PARITY)
    first = await a_started(chats, chat, "call_a", description="Gather the findings", prompt="Gather them.", retries=1)
    second = await a_started(chats, chat, "call_b", description="Summarise", prompt="Summarise the findings.",
                             after=[first["agent_id"]])
    assert second["status"] == "waiting_for_agents" and second["waiting_for"] == [first["agent_id"]]
    assert len(await children(chats, chat)) == 1

    chats.replies = [_final_response("Found three issues.")]
    await chats.wake((await children(chats, chat))[0].id)
    await tick(chats)
    started = next(c for c in await children(chats, chat) if str(c.task_id) == second["agent_id"])
    [brief] = await chats.store.get_events(started.id, types=[EventType.USER_MESSAGE])
    assert brief.data["content"] == (
        "## Reports of the agents this one waited for\n\n"
        f"### Gather the findings ({first['agent_id']})\nFound three issues.\n\n"
        "## Your task\n\nSummarise the findings."
    )


async def test_a_failed_dependency_keeps_its_dependents_waiting_and_says_so(chats):
    chat = await a_chat(chats, PARITY)
    first = await a_started(chats, chat, "call_a", description="Gather", prompt="Gather.", retries=0)
    second = await a_started(chats, chat, "call_b", description="Summarise", prompt="Summarise.", after=[first["agent_id"]])
    [child] = await children(chats, chat)
    await fail(chats, child, "provider error")
    await tick(chats)
    assert (await record_of(chats, UUID(second["agent_id"]))).status == "todo" and len(await children(chats, chat)) == 1
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["waiting_on_it"] == [second["agent_id"]]
    assert notification_note(note.data)["content"].endswith(
        "[1 agent waits for it and will not start: stop it or start a replacement.]")


async def test_after_names_only_agents_the_caller_started_in_the_background(chats):
    # Three calls, each refused the same way, nothing created: an id of another chat's background agent,
    # this chat's foreground agent (started with wake_children_as_they_come), and a random uuid.
    ...


async def test_an_agent_that_failed_cannot_be_waited_for_even_before_the_tick_settled_it(chats):
    chat = await a_chat(chats, PARITY)
    first = await a_started(chats, chat, "call_a", description="Gather", prompt="Gather.", retries=0)
    [child] = await children(chats, chat)
    await fail(chats, child, "provider error")          # no tick in between
    refused = await a_started(chats, chat, "call_b", description="Summarise", prompt="Summarise.", after=[first["agent_id"]])
    assert refused["error"] == f"agent {first['agent_id']} failed: it cannot be waited for"


async def test_a_plain_background_agent_named_in_after_keeps_its_id(chats):
    chat = await a_chat(chats, PARITY)
    plain = await a_started(chats, chat, "call_a", description="Gather", prompt="Gather.")
    [child] = await children(chats, chat)
    chats.replies = [several_calls([
        ("agent", "call_b", {"description": "B", "prompt": "B.", "run_in_background": True, "after": [plain["agent_id"]]}),
        ("agent", "call_c", {"description": "C", "prompt": "C.", "run_in_background": True, "after": [plain["agent_id"]]}),
    ]), _final_response("Queued.")]
    await chats.says(chat, "Then B and C.")
    await chats.wake(chat)
    adopted = await record_of(chats, child.id)
    assert (adopted.id, adopted.status, adopted.current_session_id, adopted.max_attempts) == (child.id, "running", child.id, 1)
    assert plain["agent_id"] == str(child.id)


async def test_a_long_report_of_a_dependency_is_cut_in_the_brief(chats):
    # As the first test, the first agent's final message "x" * 12_000.
    ...
    assert "x" * 10_000 + "\n\n## Your task" in brief.data["content"] and "x" * 10_001 not in brief.data["content"]
```

`several_calls` goes in `tests/integration/test_agent_records.py`:

```python
def several_calls(calls: list[tuple[str, str, dict]]) -> tuple[dict, dict]:
    """One assistant message with all of *calls*, each (name, call_id, arguments): they run at once."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for name, call_id, arguments in calls
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )
```

Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_dependencies.py -n 0 -p no:cacheprovider -q`
Expected: 6 failed (`after` is not an argument).

- [ ] **Step 3: Implement**

The `after` property:

```python
"after": {
    "type": "array", "items": {"type": "string"}, "maxItems": 10,
    "description": (
        "Optional agent_ids of agents you started in the background: this agent starts once all of them have "
        "completed, with their reports in front of its brief. Needs run_in_background=true."
    ),
},
```

- [ ] **Step 4: Run the new file and the family's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_dependencies.py tests/integration/test_agent_records.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Document `after?`, the status `waiting_for_agents` and `waiting_on_it` in section 13; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/harness/agent_records.py tests/integration/test_agent_dependencies.py tests/integration/test_agent_records.py docs/sub-agents/index.md
git commit -m "feat(agents): an agent starts after the agents it names have completed, with their reports"
```

---

### Task 6: Agents over the limit wait for a slot

**Files:**
- Modify: `surogates/tools/builtin/agents.py` (`_start_agent`, `_agent_handler`, `_running_agents`)
- Modify: `surogates/tasks/dispatcher.py` (`_start_ready_records`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_records.py`, `tests/integration/test_agent_tool.py`

**Interfaces:**
- Consumes: `create_record`, `start_record`, the claim of Task 4.
- Produces: a background call over the limit returns `{"agent_id": <record id>, "status": "queued", "description": ..., "note": QUEUED_NOTE}`.
- Produces: `QUEUED_NOTE`, built where it is used: `f"It starts when one of your running agents ends: {MAX_RUNNING_IN_BACKGROUND} already run in the background."`

Behaviour:
- The handler counts, against the limit, the parent's running background sessions, its starts under way in this process (`_STARTING`), and its records `running` with no `current_session_id` (claimed by a tick, not yet started). A background call that finds the limit reached leaves its record `ready` and answers `queued`: a plain call gets a record for this (`start_now=False`), a call with `after` or `retries` already has one. A `fork` over the limit is still refused with today's error.
- The tick's claim (Task 4) adds, under the same per-parent advisory lock: the parent session's status is not `paused`, `failed` or `archived`; fewer than `agents.MAX_RUNNING_IN_BACKGROUND` of the parent's sessions are `active` or `processing` with dispatch mode `background`, counting its records `running` with no session; and a record whose `agent_def_name` is in `delegate._NON_RECURSIVE_AGENT_TYPES` is not claimed while the parent runs a session of that agent type (today's one-at-a-time rule).
- The limit is exact for the starts of one process and for the tick's claims. A start in the parent's own call and a claim by a tick on another worker at the same moment can pass it by one: state this in section 13.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_sixth_background_agent_is_queued_and_starts_when_one_ends(chats):
    chat = await a_chat(chats, PARITY)
    for n in range(5):
        await a_started(chats, chat, f"call_{n}", description=f"Part {n}", prompt=f"Do part {n}.")
    sixth = await a_started(chats, chat, "call_5", description="Part 5", prompt="Do part 5.")
    assert sixth["status"] == "queued" and "5 already run in the background" in sixth["note"]
    await tick(chats)
    assert len(await children(chats, chat)) == 5

    first = (await children(chats, chat))[0]
    chats.replies = [_final_response("Part done.")]
    await chats.wake(first.id)
    await tick(chats)
    assert len(await children(chats, chat)) == 6


async def test_six_agents_started_in_one_response_run_five_and_queue_one(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [several_calls([
        ("agent", f"call_{n}", {"description": f"Part {n}", "prompt": f"Do part {n}.", "run_in_background": True})
        for n in range(6)
    ]), _final_response("Started.")]
    await chats.says(chat, "Do all six parts.")
    await chats.wake(chat)
    statuses = sorted(json.loads(await tool_result(chats, chat, f"call_{n}"))["status"] for n in range(6))
    assert statuses == ["queued"] + ["running"] * 5 and len(await children(chats, chat)) == 5


@pytest.mark.parametrize("status", ["paused", "failed", "archived"])
async def test_no_agent_starts_for_a_parent_that_is_stopped_failed_or_deleted(chats, status):
    chat = await a_chat(chats, PARITY)
    await a_record(chats, chat)
    await chats.store.update_session_status(chat, status)
    await tick(chats)
    assert await children(chats, chat) == []
    if status == "paused":
        await chats.store.update_session_status(chat, "completed")
        await tick(chats)
        assert len(await children(chats, chat)) == 1


async def test_a_claimed_record_counts_against_the_limit(chats, monkeypatch):
    monkeypatch.setattr(agents_module, "MAX_RUNNING_IN_BACKGROUND", 1)
    chat = await a_chat(chats, PARITY)
    await a_record(chats, chat, start_now=True)        # claimed, not started yet
    queued = await a_started(chats, chat, "call_a", description="A", prompt="A.")
    assert queued["status"] == "queued" and await children(chats, chat) == []


async def test_a_second_agent_of_a_one_at_a_time_type_waits_for_the_first(chats):
    # Two records with agent_def_name "deep-research" for one chat, the agent having that type;
    # the tick starts one; the second starts once the first's session has ended.
    ...
```

In `tests/integration/test_agent_tool.py`, `test_a_session_runs_at_most_five_agents_in_the_background` sets the limit to 2 and starts three: change it to expect `"status": "queued"` for the third and two children, and add a case that a third started with `fork=true` still gets the error `2 of your agents already run in the background`. Write the body marked `...` in full, with the agent type's `AGENT.md` provided the way `tests/integration/test_agent_tool.py` provides one if it does, else by monkeypatching `resolve_agent_by_name`.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q`
Expected: the new tests and the changed one fail (the "already run in the background" error; the tick starts records of a stopped parent).

- [ ] **Step 3: Implement the behaviours**

- [ ] **Step 4: Run the same files and `tests/integration/test_send_message.py` (its resume-and-start test shares the limit)**

Expected: all pass.

- [ ] **Step 5: Document the status `queued` and the limit's one-start tolerance in section 13; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/tasks/dispatcher.py tests/integration/test_agent_records.py tests/integration/test_agent_tool.py docs/sub-agents/index.md
git commit -m "feat(agents): a background agent over the limit waits for a slot"
```

---

### Task 7: Retries

**Files:**
- Modify: `surogates/harness/agent_records.py` (`brief_for`, `tokens_left`)
- Modify: `surogates/tools/builtin/agents.py` (`start_record`, `_agent_handler`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_retries.py`

**Interfaces:**
- Produces, in `agent_records.py`: `async def tokens_left(session_factory, session_store, record: Task) -> int | None` (None for no budget).

Behaviour:
- `brief_for` adds the "Earlier attempts" section for a record with sessions before its current one (`sessions.task_id` = the record, oldest first): each attempt's outcome from `classify_run`, and its last words, cut to 500 characters, from the last `llm.response` of `run_of` its events, else `it ended without a report`.
- The token budget covers all attempts. The handler stores the budget it computes (`child_tokens`) in `agent_dispatch["tokens"]`. `tokens_left` is that minus `store.session_tree_tokens` of every session with `task_id` = the record. `start_record` gives the attempt `tokens_left` as its `AGENT_BUDGET_KEY`; when it is 0 or less it raises `RecordNotStartable("its token budget is used up")`, so the record fails with a notification.
- A stop is not retried (Task 4 settles it `cancelled`).

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_failed_attempt_is_started_again_from_the_brief_with_what_went_before(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_a", description="Check the totals", prompt="Check the totals.", retries=1)
    [first] = await children(chats, chat)
    # Its last words, with no turn of it completed: a completed turn would report to the parent.
    await chats.store.emit_event(first.id, EventType.LLM_RESPONSE, {
        "message": {"role": "assistant", "content": "The file is locked."},
    })
    await fail(chats, first, "provider error")
    await tick(chats)
    second = next(c for c in await children(chats, chat) if c.id != first.id)
    record = await record_of(chats, UUID(started["agent_id"]))
    assert (record.status, record.attempt_count, record.current_session_id) == ("running", 2, second.id)
    [brief] = await chats.store.get_events(second.id, types=[EventType.USER_MESSAGE])
    assert brief.data["content"] == (
        "## Earlier attempts\n\n- Attempt 1 (failed): The file is locked.\n\n## Your task\n\nCheck the totals."
    )


async def test_a_stopped_agent_is_not_started_again(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_a", description="Check", prompt="Check.", retries=2)
    chats.replies = [calling("stop_agent", "call_stop", agent_id=started["agent_id"]), _final_response("Stopped.")]
    await chats.says(chat, "Stop it.")
    await chats.wake(chat)
    await tick(chats)
    assert (await record_of(chats, UUID(started["agent_id"]))).status == "cancelled"
    assert len(await children(chats, chat)) == 1
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert (note.data["agent_id"], note.data["status"]) == (started["agent_id"], "killed")


async def test_a_message_reaches_the_attempt_that_runs_now(chats):
    # One failed attempt, a tick, then send_message(to=<record id>): written to the second attempt's log.
    # (Passes once Task 4 is done: it guards the one-id rule across attempts.)
    ...


async def test_the_token_budget_covers_every_attempt(chats):
    # agent_token_budget 1000 on the chat; the first attempt's session used 600 tokens and failed; the tick runs.
    ...
    assert second.config[AGENT_BUDGET_KEY] == {"tokens": 400}
```

Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py -n 0 -p no:cacheprovider -q`
Expected: the brief and budget tests fail; the stop and message tests pass (they guard what Task 4 built).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file, the records', the budget's and the task layer's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py tests/integration/test_agent_records.py tests/integration/test_agent_budget.py tests/integration/tasks -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add a "Retries" subsection to section 13 (fresh start, earlier attempts before the brief, one id, the budget over all attempts, a stop is not retried); commit**

```bash
git add surogates/harness/agent_records.py surogates/tools/builtin/agents.py tests/integration/test_agent_retries.py docs/sub-agents/index.md
git commit -m "feat(agents): a retry starts from the brief with the earlier attempts, within one token budget"
```

---

### Task 8: A recorded result

**Files:**
- Modify: `surogates/session/events.py` (`AGENT_RESULT = "agent.result"`)
- Modify: `surogates/tools/builtin/agents.py` (new tool `record_result`, `register`)
- Modify: `surogates/tools/router.py` (`TOOL_LOCATIONS`: `"record_result": ToolLocation.HARNESS`)
- Modify: `surogates/harness/subagents.py` (`RESULT_TOOL`, `PARITY_TOOLS`, `apply_parity`, `recorded_result`, `tell_parent`)
- Modify: `surogates/harness/agent_news.py`, `surogates/tools/builtin/delegate.py` (the foreground result), `surogates/harness/agent_records.py` (`settle` writes `result_metadata`)
- Modify: every other list that names `list_agents` as one of the family's tools (find them with `git grep -n list_agents -- surogates`), where `record_result` applies.
- Modify: `tests/integration/test_agent_tool.py` (`REAL`, `wake_children_as_they_come`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_results.py`

**Interfaces:**
- Produces: tool `record_result(data: object)`, in `PARITY_TOOLS`, offered to every session with `agent_dispatch` and to no other; its handler is `agents._record_result_handler(arguments: dict, **kwargs) -> str`.
- Produces, in `subagents.py`: `RESULT_TOOL = "record_result"`, `MAX_RECORDED_RESULT_CHARS = 20_000`, `def recorded_result(events: list[Any]) -> dict | None` (the `data` of the last `agent.result` event in `run_of(events)`).
- Produces: `agent.notification` payload key `metadata`; `tasks.result_metadata` for a record.
- Produces: `wake_children_as_they_come(chats, chat, replies)` accepts, for each child, a reply or a list of replies.

Behaviour:
- `apply_parity` adds `record_result` and `send_message` to a session with `agent_dispatch` whatever its `allowed_tools` (it keeps honouring an explicit `excluded_tools`), as `WORKER_SELF_TOOLS` are added to a task worker: a sub-agent started with a tool list must still be able to report and to ask. A session without the flag never gets `record_result`.
- The handler writes an `agent.result` event `{"data": data}` on the caller's log and answers `{"status": "recorded"}`. A later call in the same run replaces the earlier one.
- Errors: the caller has no `agent_dispatch`: `only a sub-agent records a result`. `data` is not a JSON object: `data must be an object`. Its JSON is over 20,000 characters: `the result is too large: 20000 characters at most`.
- `tell_parent` adds `metadata` when `recorded_result` is not None. `notification_note` puts `[Recorded result: <json, sorted keys>]` on the line after the head. A foreground call's result gets `\n\n[Recorded result: <json, sorted keys>]` after the trace summary and before its `[agent_id: …]`. `settle` writes it to `result_metadata` when a record is `done`, and clears it when the run recorded none.

The tool's schema:

```python
_RECORD_RESULT_SCHEMA = ToolSchema(
    name="record_result",
    description=(
        "Record structured data with your result, for the agent that started you to check: counts, verdicts, "
        "lists. Your final message is still your report; this adds the data to it. A later call replaces an "
        "earlier one."
    ),
    parameters={
        "type": "object",
        "properties": {"data": {"type": "object", "description": "A JSON object."}},
        "required": ["data"],
        "additionalProperties": False,
    },
)
```

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_sub_agents_recorded_result_reaches_its_parent_and_its_record(chats):
    chat = await a_chat(chats, PARITY)
    started = await a_started(chats, chat, "call_a", description="Verify the build", prompt="Run the checks.",
                              retries=1, tools=["read_file"])
    [child] = await children(chats, chat)
    chats.replies = [
        calling("record_result", "call_r", data={"tests_run": 12, "failed": 0}),
        _final_response("All twelve checks pass."),
    ]
    await chats.wake(child.id)
    assert {"record_result", "send_message"} <= set(chats.offered[-1])   # despite the tool list
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["metadata"] == {"tests_run": 12, "failed": 0}
    assert notification_note(note.data)["content"] == (
        f"[Agent {started['agent_id']} (Verify the build) completed]\n"
        '[Recorded result: {"failed": 0, "tests_run": 12}]\nAll twelve checks pass.'
    )
    await tick(chats)
    record = await record_of(chats, UUID(started["agent_id"]))
    assert (record.result, record.result_metadata) == ("All twelve checks pass.", {"tests_run": 12, "failed": 0})


async def test_a_foreground_agents_recorded_result_is_in_the_calls_result(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [calling("agent", "call_agent", description="Verify", prompt="Verify."), _final_response("Done.")]
    waker = await wake_children_as_they_come(chats, chat, [[
        calling("record_result", "call_r", data={"ok": True}), _final_response("Verified."),
    ]])
    try:
        await chats.says(chat, "Verify it.")
        await chats.wake(chat)
    finally:
        waker.cancel()
    [child] = await children(chats, chat)
    result = await tool_result(chats, chat, "call_agent")
    assert result.endswith(f'[Recorded result: {{"ok": true}}]\n\n[agent_id: {child.id}]')


@pytest.mark.parametrize("data, error", [
    ([1, 2], "data must be an object"),
    ({"blob": "x" * 20_001}, "the result is too large: 20000 characters at most"),
])
async def test_a_result_that_is_no_object_or_too_large_is_refused(chats, data, error):
    chat = await a_chat(chats, PARITY)
    await a_started(chats, chat, "call_a", description="Verify", prompt="Verify.")
    [child] = await children(chats, chat)
    chats.replies = [calling("record_result", "call_r", data=data), _final_response("Done.")]
    await chats.wake(child.id)
    assert json.loads(await tool_result(chats, child.id, "call_r"))["error"] == error
    assert await chats.store.get_events(child.id, types=[EventType.AGENT_RESULT]) == []


async def test_a_chat_is_not_offered_the_tool_and_its_handler_refuses_one(chats):
    chat = await a_chat(chats, PARITY)
    await chats.says(chat, "Hello.")
    await chats.wake(chat)
    assert "record_result" not in chats.offered[-1]
    answer = await agents_module._record_result_handler(
        {"data": {"a": 1}}, session_store=chats.store, session_id=chat, tool_call_id="call_r",
    )
    assert json.loads(answer)["error"] == "only a sub-agent records a result"


async def test_a_run_that_records_nothing_clears_what_an_earlier_run_recorded(chats):
    # retries=1; the agent records {"a": 1} and completes; the tick runs; the chat messages it;
    # its second run records nothing; the tick runs.
    ...
    assert "metadata" not in second_note.data and (await record_of(chats, record_id)).result_metadata is None
```

Add `"record_result"` to `REAL` in `tests/integration/test_agent_tool.py`, and let `wake_children_as_they_come` take a list of replies for a child (inserted in order at the front of `chats.replies`). Write the body marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q`
Expected: all fail (`record_result` is not offered; no `AGENT_RESULT`).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file, the family's, and the test files that pin tool routing**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py tests/integration/test_agent_tool.py tests/integration/test_agent_retries.py $(git grep -l TOOL_LOCATIONS -- tests) -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add `record_result` to the tools table of section 13 and `metadata` to the notification's fields; commit**

```bash
git add surogates/session/events.py surogates/tools/builtin/agents.py surogates/tools/router.py surogates/harness/subagents.py surogates/harness/agent_news.py surogates/tools/builtin/delegate.py surogates/harness/agent_records.py tests/integration/test_agent_results.py tests/integration/test_agent_tool.py docs/sub-agents/index.md
git commit -m "feat(agents): a sub-agent records structured data with its result"
```

Add to that `git add` any file the `list_agents` search made you change.

---

### Task 9: Waiting for an answer

**Files:**
- Modify: `surogates/session/events.py` (`AGENT_WAITING = "agent.waiting"`)
- Modify: `surogates/tools/builtin/agents.py` (`_SEND_MESSAGE_SCHEMA`, `_send_message_handler`, `_stop_agent_handler`)
- Modify: `surogates/harness/subagents.py` (`asked_parent`, `tell_parent`), `surogates/harness/agent_news.py`
- Modify: `surogates/harness/agent_records.py` (`classify_run`, `settle`, `cancel`)
- Modify: `surogates/tools/builtin/delegate.py` (the foreground result)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_results.py`

**Interfaces:**
- Produces: `send_message` argument `needs_reply` (boolean).
- Produces, in `subagents.py`: `def asked_parent(events: list[Any]) -> str | None`: the `summary` of the last `agent.waiting` event in `run_of(events)`, or None.
- Produces: notification status `waiting_for_answer`; `classify_run` returns `RunOutcome.WAITING` for a `completed` session whose run `asked_parent`.
- Produces, in `agent_records.py`: `async def cancel(session_factory, record_id: UUID, parent_session_id: UUID) -> str | None`: a record of that parent that is `todo`, `ready` or `blocked` becomes `cancelled` (`completed_at` now); returns its status before, or None when it was none of these or not that parent's.

Behaviour:
- `needs_reply=true` is valid only in a message to `"parent"` from a session with `agent_dispatch`. Otherwise the call fails with `needs_reply is for a message to the agent that started you`, and nothing is sent.
- With it, the handler also writes `agent.waiting` `{"summary": summary, "tool_call_id": ...}` on the sender's log, and its note is `Delivered. End your turn now: you resume when the answer comes.`
- When that run ends `completed`: a background agent's notification has status `waiting_for_answer` and its head is `[Agent <id> (<description>) is waiting for your answer: reply to it with send_message]`; no `delegation.complete` is written. A foreground call's result gets `\n\n[It is waiting for your answer: reply to it with send_message.]` before its `[agent_id: …]`.
- `settle` settles such a record as `blocked` with `blocked_reason` the summary: it is not `done`, so its dependents stay `todo`, and `attempt_count` is not touched. Re-opening it when the answer resumes it is Task 10's.
- `stop_agent` given a record that is `blocked` (its session is `completed`) cancels it with `cancel`, answers `{"status": "stopped", "agent_id": <id>, "note": "It was waiting for an answer: it will not resume."}`, and calls `announce_end(..., "killed")`, so the parent hears which agents wait on it.

The property:

```python
"needs_reply": {
    "type": "boolean",
    "description": (
        "true, in a message to your parent only: you cannot go on without its answer. End your turn after it; "
        "you resume when the answer comes."
    ),
},
```

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_that_asks_its_parent_waits_and_its_dependents_with_it(chats):
    chat = await a_chat(chats, PARITY)
    asker = await a_started(chats, chat, "call_a", description="Draft the memo", prompt="Draft it.", retries=1)
    after = await a_started(chats, chat, "call_b", description="Send the memo", prompt="Send it.", after=[asker["agent_id"]])
    [child] = await children(chats, chat)
    chats.replies = [
        calling("send_message", "call_ask", to="parent", message="Which quarter?", summary="Which quarter", needs_reply=True),
        _final_response("I need the quarter to go on."),
    ]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["status"] == "waiting_for_answer"
    assert notification_note(note.data)["content"].startswith(
        f"[Agent {asker['agent_id']} (Draft the memo) is waiting for your answer: reply to it with send_message]")
    await tick(chats)
    record = await record_of(chats, UUID(asker["agent_id"]))
    assert (record.status, record.blocked_reason, record.attempt_count) == ("blocked", "Which quarter", 1)
    assert (await record_of(chats, UUID(after["agent_id"]))).status == "todo" and len(await children(chats, chat)) == 1


async def test_an_agent_waiting_for_an_answer_can_be_stopped_and_its_dependents_are_named(chats):
    # As above up to the blocked record; the chat calls stop_agent(<asker id>); the tick runs.
    ...
    assert stopped == {"status": "stopped", "agent_id": asker["agent_id"],
                       "note": "It was waiting for an answer: it will not resume."}
    assert (await record_of(chats, UUID(asker["agent_id"]))).status == "cancelled"
    assert notes[-1].data["waiting_on_it"] == [after["agent_id"]]


@pytest.mark.parametrize("to", ["sibling", "from a chat"])
async def test_needs_reply_is_only_for_a_sub_agents_message_to_its_parent(chats, to):
    ...
    assert json.loads(result)["error"] == "needs_reply is for a message to the agent that started you"


async def test_a_question_of_an_earlier_run_does_not_make_a_later_run_wait(chats):
    # The agent asked; the chat answers with send_message (it resumes); its second run ends without asking.
    ...
    assert notes[-1].data["status"] == "completed"


async def test_a_foreground_agent_that_asks_says_so_in_the_calls_result(chats):
    # wake_children_as_they_come with [send_message(needs_reply=True), final response] for the child.
    ...
    assert result.endswith(f"[It is waiting for your answer: reply to it with send_message.]\n\n[agent_id: {child.id}]")
```

Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q -k "asks or needs_reply or earlier_run or waiting_for_an_answer"`
Expected: all fail (`needs_reply` is not an argument).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file and the files of the messages, the stops and the dependencies**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py tests/integration/test_send_message.py tests/integration/test_list_stop_agents.py tests/integration/test_agent_dependencies.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Document `needs_reply?`, the status `waiting_for_answer` and stopping a waiting agent in section 13; commit**

```bash
git add surogates/session/events.py surogates/tools/builtin/agents.py surogates/harness/subagents.py surogates/harness/agent_news.py surogates/harness/agent_records.py surogates/tools/builtin/delegate.py tests/integration/test_agent_results.py docs/sub-agents/index.md
git commit -m "feat(agents): a sub-agent that needs an answer waits for it, and its dependents with it"
```

---

### Task 10: An agent that runs again

**Files:**
- Modify: `surogates/harness/agent_records.py` (`reopen`)
- Modify: `surogates/tasks/dispatcher.py` (new `_reopen_resumed_records`, `tasks_tick`)
- Modify: `surogates/tools/builtin/agents.py` (`_send_message_handler`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_retries.py`, `tests/integration/test_agent_results.py`

**Interfaces:**
- Produces, in `agent_records.py`: `async def reopen(session_factory, record_id: UUID) -> bool`: in one transaction, a record that is `done`, `failed`, `cancelled` or `blocked` becomes `running`, with `completed_at` and `blocked_reason` cleared and `max_attempts` set to `attempt_count`; and every record that names it in a `TaskLink` and is `ready` goes back to `todo`. Returns whether it re-opened.
- Produces: `tasks_tick` returns `reopened`.

Behaviour:
- Setting `max_attempts` to `attempt_count` is what keeps a run resumed by a message from being retried from the brief: a fresh start would drop the message that resumed it, the parent's answer included. Its failure is announced (Task 4) and the parent decides.
- `_send_message_handler`, where it resumes a resting agent that has a record, calls `reopen` right after `resume_if_resting`. The tick catches every other resume, and a crash between the two: `_reopen_resumed_records` re-opens each record that is `done`, `failed`, `cancelled` or `blocked` whose `current_session_id` session is `active` or `processing` (a user typing into a sub-agent's chat resumes it too). It runs in `tasks_tick` after recovery and before settle. Filter on `agent_dispatch IS NOT NULL`.
- When the resumed run ends, `settle` settles it as any run, so its report and recorded result replace the earlier ones, and a dependency that now completes releases what waited on it. Records that had already started are not touched.
- `send_message` to a record between attempts (`todo`, `ready`, or `running` with no session yet) fails with `agent <id> has not started yet: stop it and start it again with the new brief`; before that check the handler settles the record (`settle(record_ids=[...])`), so a failed attempt waiting for its retry is seen as `ready`, not resumed by the message while the tick starts the next one.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_finished_agent_asked_to_work_again_reports_anew_and_its_record_follows(chats):
    # retries=1; it records {"failed": 2} and completes; the tick runs; the chat messages it "check again";
    # its second run records {"failed": 0} and completes "All pass now."; the tick runs.
    ...
    assert (reopened.status, reopened.completed_at, reopened.max_attempts) == ("running", None, 1)
    assert (final.status, final.result, final.result_metadata) == ("done", "All pass now.", {"failed": 0})


async def test_a_resumed_run_that_fails_is_not_started_again(chats):
    # retries=2; it completes; the tick runs; the chat messages it; the resumed run fails; the tick runs.
    ...
    assert (record.status, record.attempt_count, len(await children(chats, chat))) == ("failed", 1, 1)
    assert notes[-1].data["status"] == "failed"


async def test_an_answer_that_resumes_a_waiting_agent_is_not_lost_to_a_retry(chats):
    # retries=2; it asks with needs_reply and the tick blocks it; the chat answers; the resumed run fails; the tick runs.
    ...
    assert (record.status, len(await children(chats, chat))) == ("failed", 1)


async def test_a_dependency_that_completes_after_a_resume_releases_what_waited(chats):
    # A (no retries) fails; B after A stays todo; the chat messages A, which resumes and completes; the tick runs.
    ...
    assert (await record_of(chats, UUID(b["agent_id"]))).status == "running"


async def test_a_resume_moves_a_dependent_waiting_for_a_slot_back_to_waiting(chats, monkeypatch):
    # Limit 1; A completes; B after A is promoted to ready but waits for the slot C holds; the chat messages A.
    ...
    assert (await record_of(chats, UUID(b["agent_id"]))).status == "todo"


async def test_a_user_who_resumes_a_sub_agent_reopens_its_record_through_the_tick(chats):
    # retries=1; it completes; the tick runs; its session is resumed as a user's message resumes it
    # (resume_if_resting, then a user message); the tick runs.
    ...
    assert (await record_of(chats, record_id)).status == "running"


async def test_a_message_to_an_agent_between_attempts_is_refused(chats):
    # retries=1; its first attempt fails; no tick; the chat calls send_message(to=<record id>).
    ...
    assert "has not started yet" in json.loads(await tool_result(chats, chat, "call_message"))["error"]
    assert len(await children(chats, chat)) == 1


async def test_a_dependent_that_already_runs_is_not_touched_by_a_resume(chats):
    # A completes; B after A runs; the chat messages A, which resumes. (Guards the rule: passes before reopen exists.)
    ...
    assert (await record_of(chats, UUID(b["agent_id"]))).status == "running"
```

All go in `test_agent_retries.py`. Write the bodies in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py -n 0 -p no:cacheprovider -q -k "again or resume or between or answer or user_who"`
Expected: all fail except `test_a_dependent_that_already_runs_is_not_touched_by_a_resume`, which guards a rule and passes already.

- [ ] **Step 3: Implement `reopen`, `_reopen_resumed_records` and the two changes in `_send_message_handler`**

- [ ] **Step 4: Run the retries', the messages', the dependencies' and the results' files**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py tests/integration/test_send_message.py tests/integration/test_agent_dependencies.py tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add "An agent that runs again" to section 13 (the rules of the spec's section of that name); commit**

```bash
git add surogates/harness/agent_records.py surogates/tasks/dispatcher.py surogates/tools/builtin/agents.py tests/integration/test_agent_retries.py docs/sub-agents/index.md
git commit -m "feat(agents): an agent that runs again re-opens its record"
```

---

### Task 11: Stop and list agents that have not started

**Files:**
- Modify: `surogates/harness/agent_records.py` (`records_of`)
- Modify: `surogates/tools/builtin/agents.py` (`_stop_agent_handler`, `_list_agents_handler`, `_LIST_AGENTS_SCHEMA`, `_STOP_AGENT_SCHEMA`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_list_stop_agents.py`

**Interfaces:**
- Consumes: `cancel` (Task 9), `announce_end` (Task 4).
- Produces, in `agent_records.py`: `async def records_of(session_factory, parent_session_id: UUID) -> list[Task]` (the parent's records, oldest first).

Behaviour:
- `stop_agent` with the id of a record that is `todo` or `ready` cancels it with `cancel` and answers `{"status": "stopped", "agent_id": <id>, "note": "It had not started: it will not start."}`. A cancelled record never starts, even when its dependencies complete.
- `list_agents` lists each of the caller's records once, under the record's id, with `status` from `RECORD_STATUS`, `description`, `mode: "background"`, and `waiting_for` (the ids of its dependencies that are not `done`) when it is `waiting_for_agents`. A started agent with a record is not listed a second time under its session's id. Records `done`, `failed` or `cancelled` that never had a session are listed too.
- `_LIST_AGENTS_SCHEMA`'s description names the statuses: `running, completed, failed, stopped, queued, waiting_for_agents, waiting_for_answer`. `_STOP_AGENT_SCHEMA`'s says an agent that has not started, or waits for an answer, can be stopped too.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_that_waits_is_listed_stopped_and_never_starts(chats):  # noqa: F811
    chat = await a_chat(chats, PARITY)
    first = await a_started(chats, chat, "call_a", description="Gather", prompt="Gather.", retries=1)
    second = await a_started(chats, chat, "call_b", description="Summarise", prompt="Summarise.", after=[first["agent_id"]])
    chats.replies = [calling("list_agents", "call_list"), calling("stop_agent", "call_stop", agent_id=second["agent_id"]),
                     _final_response("Stopped.")]
    await chats.says(chat, "List them, then stop the second.")
    await chats.wake(chat)
    listed = {a["agent_id"]: a for a in json.loads(await tool_result(chats, chat, "call_list"))["agents"]}
    assert set(listed) == {first["agent_id"], second["agent_id"]}
    assert listed[first["agent_id"]]["status"] == "running"
    assert listed[second["agent_id"]] == {
        "agent_id": second["agent_id"], "relation": "yours", "status": "waiting_for_agents",
        "description": "Summarise", "mode": "background", "waiting_for": [first["agent_id"]],
    }
    assert json.loads(await tool_result(chats, chat, "call_stop")) == {
        "status": "stopped", "agent_id": second["agent_id"], "note": "It had not started: it will not start.",
    }
    [child] = await children(chats, chat)
    chats.replies = [_final_response("Gathered.")]
    await chats.wake(child.id)
    await tick(chats)
    assert (await record_of(chats, UUID(second["agent_id"]))).status == "cancelled" and len(await children(chats, chat)) == 1


async def test_a_queued_agent_is_stopped_the_same_way(chats, monkeypatch):  # noqa: F811
    # Limit 1; a second background agent is queued; stop_agent(<its id>); the first ends; the tick runs.
    ...
    assert (await record_of(chats, UUID(queued["agent_id"]))).status == "cancelled" and len(await children(chats, chat)) == 1


async def test_an_agent_cannot_stop_another_sessions_waiting_agent(chats):  # noqa: F811
    # Guards a rule: passes before the change.
    ...
    assert "no agent" in json.loads(await tool_result(chats, other, "call_stop"))["error"]
    assert (await record_of(chats, UUID(waiting["agent_id"]))).status == "todo"
```

Import `tick`, `record_of` and `a_started` from `.test_agent_records`. Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_list_stop_agents.py -n 0 -p no:cacheprovider -q -k "waits or queued or another_sessions"`
Expected: the first two fail (`no agent <id> that you started`); the third passes.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run every test file of the family**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_tool.py tests/integration/test_agent_records.py tests/integration/test_agent_dependencies.py tests/integration/test_agent_retries.py tests/integration/test_agent_results.py tests/integration/test_send_message.py tests/integration/test_list_stop_agents.py tests/integration/test_agent_budget.py tests/integration/test_monitor.py tests/integration/tasks tests/test_delegate.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Update the `list_agents` and `stop_agent` rows of section 13; commit**

```bash
git add surogates/harness/agent_records.py surogates/tools/builtin/agents.py tests/integration/test_list_stop_agents.py docs/sub-agents/index.md
git commit -m "feat(agents): list and stop agents that wait or queue"
```

---

## When every task is done

1. Run the lint check on the changed Python files with any ruff (there is one at `/work/surogate-ops/.venv/bin/ruff`): `ruff check --no-cache --select F,E9 $(git diff --name-only origin/master -- '*.py')`. Expected: nothing new against `origin/master`.
2. With the flag off, the old behaviour is untouched: run `tests/integration/tasks`, `tests/integration/missions`, `tests/test_delegate.py` and `tests/integration/test_workstream_threads.py`. Expected: the same results as on `origin/master`.
3. One fresh reviewer reads the whole branch against the spec and this plan.
4. Open one pull request against `master`. It changes nothing on PROD, where the flag is off.
