# Agent Family Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, natively in one session; one fresh reviewer checks the whole branch at the end (Flavius's standing rule: no subagent-driven development). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `agent` tool family what the old delegation mechanisms have and it lacks: a per-call tool list, dependencies, queueing over the background limit, retries, recorded results, waiting for an answer, re-opening on resume, and stop/list for agents that have not started.

**Architecture:** A background agent that needs any of these gets a *record*: a row in the existing `tasks` table with a new `agent_dispatch` column set. The task dispatcher tick (`surogates/tasks/dispatcher.py`), which already promotes, starts and finalizes rows crash-safely, does the same for records, but starts them through the family's own start path (`delegate._run_single_delegation`, channel `delegation`). A plain `agent` call gets no record and behaves as today. Everything stays behind `agents.subagent_parity`, which is off on PROD.

**Tech Stack:** Python 3.12, async SQLAlchemy on Postgres, Redis, pytest with testcontainers for integration tests.

**Spec:** `docs/superpowers/specs/2026-11-12-agent-family-only-design.md`

**Where this sits:** the spec is delivered by three plans. This is the first. The second moves the consumers onto the family with the flag on (missions, deep research, project threads, Keel, governance). The third removes the flag and the old tools and changes the other repositories. Each later plan is written when the one before it has landed, because it builds on the names this one produces.

## Global Constraints

- With `agents.subagent_parity` off, nothing changes: the tools offered and every existing test stay as they are. Do not touch `delegate_task`, `spawn_worker`, `spawn_task`, the `worker_*` tools or the thread tools.
- A plain `agent` call (no `after`, no `retries`, under the limit) creates no record and returns the child's session id as `agent_id`, as today.
- `MAX_RUNNING_IN_BACKGROUND` stays 5. The depth limit stays 2. `retries` is 0 to 5, default 0.
- Reports and notification texts are cut to `MAX_RESULT_CHARS` (10,000). A recorded result is at most 20,000 characters of JSON.
- A new builtin tool needs an entry in `TOOL_LOCATIONS` (`surogates/tools/router.py`), or it answers "Unknown tool".
- Schema changes go in `surogates/db/models.py` and, as idempotent SQL, in `surogates/db/observability.sql`. There is no Alembic here.
- Read the agent and org from the session, never from `SUROGATES_AGENT_ID` / `SUROGATES_ORG_ID`.
- Tool descriptions stay general: no product names, no file names.
- No plan or task numbers in code comments or commit messages. Conventional commits. No `Co-Authored-By` trailer.
- Work in a git worktree outside `/work` and outside `/tmp` (`git -C /work/surogates worktree add -b feat/agent-family-capabilities ~/worktrees/surogates-agent-family origin/master`). Never check out or pull in `/work/surogates`.
- In the worktree, make its own venv once: `uv sync --frozen --python 3.12 --all-extras`. Run only the affected test files, one worker: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest <files> -n 0 -p no:cacheprovider -q`.
- Keep `docs/sub-agents/index.md` section 13 in step: each task that adds an argument, a tool or a status adds it to that section's tables.

## Review Focus

1. A worker dies after a record is claimed and before its session exists. Expected: the agent still starts. A claimed record with no session for 60 seconds goes back to `ready` (test in Task 3).
2. The parent is stopped by its user, failed or deleted while its agents wait or queue. Expected: none of them starts; they start once the parent is resumed (test in Task 5).
3. Six background agents are started in one response, whose calls run at once. Expected: five run and one is queued, never six (test in Task 5).
4. An old `spawn_task` row and a record are both ready in the same tick. Expected: each starts by its own path; the record's child is offered no `worker_*` tool and the old row's child still is (test in Task 3).
5. A recorded result that is not an object or is over 20,000 characters, and a dependency's report over 10,000 characters. Expected: the first is refused with a clear error, the second is cut, and neither breaks a start (tests in Tasks 4 and 7).

---

## File Structure

| File | Responsibility |
|---|---|
| `surogates/harness/agent_records.py` (new) | The record behind an agent: create, resolve an id, claim/release, adopt, build the brief, classify a run's end, re-open, cancel an unstarted one. |
| `surogates/tools/builtin/agents.py` | The tools: new arguments of `agent` and `send_message`, the `record_result` tool, `start_record`, stop/list with records. |
| `surogates/tools/builtin/delegate.py` | `child_tool_policy(call_tools=)`, `_run_single_delegation(task_id=)`, the foreground result's notes. |
| `surogates/harness/subagents.py` | What is offered to whom, `tell_parent` aware of records, reading a run's recorded result and its question. |
| `surogates/harness/agent_news.py` | How the new notification fields read to the model. |
| `surogates/tasks/dispatcher.py` | The tick: start, finalize, retry and recover records. |
| `surogates/db/models.py`, `surogates/db/observability.sql` | `tasks.agent_dispatch`. |
| `surogates/session/events.py` | `AGENT_RESULT`, `AGENT_WAITING`. |
| `tests/integration/test_agent_records.py` (new) | Records, start through a record, the queue, and the shared helpers `tick`, `record_of`, `a_record` and `a_started`, which the other new files import. |
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

Rules for `call_tools`: it wins over the agent type's `tools`; names in `_DELEGATION_ALWAYS_BLOCKED_TOOLS` and in the parent's `excluded_tools` are dropped; a leaf loses `leaf_tools` even when the call names them; order is kept and duplicates dropped.

- [ ] **Step 1: Write the failing tests**

In `tests/test_delegate.py`:

```python
def test_call_tools_win_over_the_agent_types_and_lose_what_the_parent_may_not_run():
    allowed, _ = child_tool_policy(
        parent_config={"excluded_tools": ["terminal"]},
        agent_def=SimpleNamespace(tools=["read_file"], disallowed_tools=None),
        role="orchestrator", leaf_tools=("delegate_task", "agent"),
        call_tools=["web_search", "terminal", "ask_user_question", "agent", "web_search"],
    )
    assert allowed == ["web_search", "agent"]


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
Expected: 3 failed (`child_tool_policy() got an unexpected keyword argument 'call_tools'`, and `allowed_tools` missing).

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
- Modify: `surogates/harness/loop.py` (the two `session.task_id is not None` checks, at the question timeout and in `_apply_execution_context_gates`), `surogates/orchestrator/worker.py` (`_filter_effective_tools`)
- Test: `tests/integration/test_agent_records.py`

**Interfaces:**
- Produces, in `surogates/db/models.py`: `Task.agent_dispatch: Mapped[Optional[dict[str, Any]]]` (JSONB, nullable). Its keys: `description`, `model`, `tools`, `tool_call_id`, `tokens`. Null means an old task-layer row.
- Produces, in `surogates/db/observability.sql`: `ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS agent_dispatch jsonb;`
- Produces, in `agent_records.py`:

```python
RECORD_KEY = "record"   # in a sub-agent's agent_dispatch: the id of its tasks row

class AgentRef(NamedTuple):
    record_id: UUID | None        # its tasks row, if it has one
    session_id: UUID | None       # its current session; None while it has not started
    parent_session_id: UUID | None   # the session that started it; None for a session nobody started
    status: str                   # a tasks status for a record, else the session's status

def agent_id_of(session: Any) -> str: ...        # the record's id for a sub-agent that has one, else the session's
def is_task_worker(session: Any) -> bool: ...    # task_id set and not started by the agent tool
async def create_record(session_factory, *, parent: Any, prompt: str, description: str, agent_type: str | None,
                        model: str | None, tools: list[str] | None, tool_call_id: str | None,
                        after: list[UUID], retries: int, tokens: int) -> Task: ...
async def resolve(session_factory, raw: str) -> AgentRef | None: ...   # raw is a record id or a session id
async def claim(session_factory, record_id: UUID) -> Task | None: ...  # ready -> running, attempt_count + 1; None if not ready
async def release(session_factory, record_id: UUID) -> None: ...       # running -> ready, the attempt given back
```

- Produces: `_run_single_delegation(..., task_id: UUID | None = None)`, passed to `create_child_session(task_id=task_id)`.

`create_record` writes `goal=prompt`, `agent_def_name=agent_type`, `max_attempts=retries + 1`, `parent_session_id=parent.id`, `org_id=parent.org_id`, and status `todo` when any record in `after` is not `done`, else `ready`, with one `TaskLink` per id in `after`. `claim` and `release` use the `UPDATE ... RETURNING` pattern of `surogates/tasks/service.py` (no `SELECT ... FOR UPDATE`: it deadlocks with the child session's insert).

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
        if not any(counts.get(key) for key in ("recovered", "promoted", "finalized", "enqueued")):
            return


async def record_of(chats, ref) -> Task | None:
    """The record with id *ref*, or whose current session is *ref* (an id or a session); None when there is none."""
    key = ref if isinstance(ref, UUID) else ref.id
    async with chats.api.app.state.session_factory() as db:
        return (await db.execute(
            select(Task).where(or_(Task.id == key, Task.current_session_id == key)),
        )).scalars().first()


async def a_started(chats, chat: UUID, call_id: str, **arguments) -> dict:
    """The chat starts an agent in the background with *arguments*; the call's result."""
    chats.replies = [calling("agent", call_id, run_in_background=True, **arguments), _final_response("Started.")]
    await chats.says(chat, "Start it.")
    await chats.wake(chat)
    return json.loads(await tool_result(chats, chat, call_id))


async def a_record(chats, chat: UUID, **over) -> Task:
    parent = await chats.session(chat)
    return await create_record(chats.api.app.state.session_factory, **{
        "parent": parent, "prompt": "Check the totals.", "description": "Check the totals", "agent_type": None,
        "model": None, "tools": None, "tool_call_id": "call_agent", "after": [], "retries": 0, "tokens": 0, **over,
    })
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


async def test_an_id_resolves_to_a_record_or_a_session_and_nothing_else(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    factory = chats.api.app.state.session_factory
    assert await resolve(factory, str(record.id)) == AgentRef(record.id, None, chat, "ready")
    assert (await resolve(factory, str(chat))).session_id == chat
    assert await resolve(factory, "not-an-id") is None and await resolve(factory, str(uuid4())) is None


async def test_a_record_is_claimed_once_and_released_with_its_attempt_given_back(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    factory = chats.api.app.state.session_factory
    claimed = await claim(factory, record.id)
    assert (claimed.status, claimed.attempt_count) == ("running", 1) and await claim(factory, record.id) is None
    await release(factory, record.id)
    again = await record_of(chats, record.id)
    assert (again.status, again.attempt_count) == ("ready", 0)


async def test_a_sub_agent_with_a_record_is_no_task_worker():
    plain = SimpleNamespace(task_id=uuid4(), config={})
    family = SimpleNamespace(task_id=uuid4(), config={DISPATCH_KEY: {RECORD_KEY: "x"}})
    assert is_task_worker(plain) and not is_task_worker(family)
    assert not is_task_worker(SimpleNamespace(task_id=None, config={}))
```

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py -n 0 -p no:cacheprovider -q`
Expected: collection error, `No module named 'surogates.harness.agent_records'`.

- [ ] **Step 3: Implement**

Add the column and the SQL line, write `agent_records.py` with the signatures above, add `task_id` to `_run_single_delegation`, and replace the three `task_id is not None` checks named under Files with `is_task_worker(session)`.

- [ ] **Step 4: Run the new file and the files of the checks you changed**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/tasks -n 0 -p no:cacheprovider -q`
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
- Modify: `surogates/tasks/dispatcher.py` (`_enqueue_ready_tasks`, new `_recover_unstarted_records`, `tasks_tick`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_records.py`

**Interfaces:**
- Consumes: `create_record`, `claim`, `release`, `resolve`, `agent_id_of`, `RECORD_KEY` (Task 2).
- Produces, in `agents.py`:

```python
class RecordNotStartable(ValueError): ...   # the record can never start: unknown agent_type, parent gone

async def start_record(record: Task, *, session_store, session_factory, redis, tenant, bundle=None,
                       file_bundle_cache=None, memory_manager=None, iteration_cap: int | None = None) -> Any:
    """Start the next attempt of *record*, already claimed, as a background agent of its parent; returns its session."""
```

- Produces, in `agent_records.py`: `async def brief_for(session_factory, session_store, record: Task) -> str` (in this task it returns `record.goal`; Tasks 4 and 6 add to it).
- Produces: `agent` argument `retries` (integer 0 to 5). A call with `retries > 0` goes through a record.
- Produces: `send_message` and `stop_agent` accept a record's id for an agent that has started, through `resolve`.

Behaviour:
- `retries > 0` needs `run_in_background=true` (error: `after and retries need run_in_background=true: a call that waits for its report cannot wait for other agents or start again`) and no `fork` (error: `a fork cannot use after or retries: start it with a brief`).
- The handler creates the record, claims it and calls `start_record` under the parent's `_start_lock`, and returns `{"agent_id": <record id>, "status": "running", "description": ..., "note": STARTED_NOTE}`. If `start_record` raises anything but `RecordNotStartable`, it releases the record (the tick starts it) and still returns the record's id with `"status": "queued"`.
- `start_record` calls `delegate._run_single_delegation` with `task_id=record.id`, the brief from `brief_for`, `agent_type=record.agent_def_name`, the dispatch's `model` and `tools`, an `on_started` that ends the call at once, and `extra_config` as `_start_agent` builds it with `RECORD_KEY: str(record.id)` added to the dispatch and mode `background`. It then sets `tasks.current_session_id`. Depth and role come from the parent's `delegation_depth`, as in `_agent_handler`. Without `iteration_cap` it uses `delegate._CHILD_MAX_ITERATIONS`. For the bundle it uses `bundle`, else `file_bundle_cache.get(parent.agent_id)`, as `surogates/tasks/spawn.py` does.
- `tell_parent` names the agent by `agent_id_of(session)`.
- In `_enqueue_ready_tasks`, a claimed row with `agent_dispatch` set starts through `start_record`; `RecordNotStartable` sets the record `failed` and tells the parent with `tell_parent`-shaped data (`status: "failed"`, `error`); any other error releases it. A row without `agent_dispatch` starts as today.
- `_recover_unstarted_records(db) -> int`: records `running` with `current_session_id IS NULL` and `started_at` older than 60 seconds go back to `ready` with the attempt given back. `tasks_tick` runs it first and reports it as `"recovered"`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_with_retries_is_started_through_a_record_and_named_by_it(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [
        calling("agent", "call_agent", description="Check the totals", prompt="Check the totals.",
                run_in_background=True, retries=2),
        _final_response("Started."),
    ]
    await chats.says(chat, "Check the totals.")
    await chats.wake(chat)
    started = json.loads(await tool_result(chats, chat, "call_agent"))
    [child] = await children(chats, chat)
    record = await record_of(chats, child)
    assert started["agent_id"] == str(record.id) != str(child.id) and started["status"] == "running"
    assert (record.status, record.attempt_count, record.max_attempts, record.current_session_id) == ("running", 1, 3, child.id)
    assert child.channel == "delegation" and child.config[DISPATCH_KEY][RECORD_KEY] == str(record.id)

    chats.replies = [_final_response("The totals match.")]
    await chats.wake(child.id)
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert (note.data["agent_id"], note.data["status"]) == (str(record.id), "completed")
    await tick(chats)
    done = await record_of(chats, record.id)
    assert (done.status, done.result) == ("done", "The totals match.")


@pytest.mark.parametrize("extra, error", [
    ({"retries": 1}, "after and retries need run_in_background=true"),
    ({"retries": 1, "run_in_background": True, "fork": True}, "a fork cannot use after or retries"),
])
async def test_retries_need_the_background_and_no_fork(chats, extra, error):
    chat = await a_chat(chats, PARITY)
    chats.replies = [calling("agent", "call_agent", description="x", prompt="Do x.", **extra), _final_response("No.")]
    await chats.says(chat, "Do x.")
    await chats.wake(chat)
    assert error in json.loads(await tool_result(chats, chat, "call_agent"))["error"]
    assert await children(chats, chat) == []


async def test_a_message_and_a_stop_reach_an_agent_by_its_records_id(chats):
    # Start one with retries=1 as above; then the chat calls send_message(to=<record id>) and stop_agent(<record id>).
    ...
    assert json.loads(await tool_result(chats, chat, "call_message"))["status"] == "delivered"
    assert json.loads(await tool_result(chats, chat, "call_stop"))["status"] == "stopped"


async def test_the_tick_starts_a_ready_record_as_a_sub_agent_of_the_family(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    await tick(chats)
    [child] = await children(chats, chat)
    assert child.channel == "delegation" and child.task_id == record.id
    chats.replies = [_final_response("Done.")]
    await chats.wake(child.id)
    assert not WORKER_SELF_TOOLS & set(chats.offered[-1])


async def test_a_record_claimed_by_a_worker_that_died_is_started_by_a_later_tick(chats):
    chat = await a_chat(chats, PARITY)
    record = await a_record(chats, chat)
    factory = chats.api.app.state.session_factory
    await claim(factory, record.id)
    async with factory() as db:
        await db.execute(update(Task).where(Task.id == record.id).values(started_at=func.now() - timedelta(seconds=61)))
        await db.commit()
    await tick(chats)
    [child] = await children(chats, chat)
    recovered = await record_of(chats, record.id)
    assert (recovered.status, recovered.attempt_count, recovered.current_session_id) == ("running", 1, child.id)


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

Write the body of `test_a_message_and_a_stop_reach_an_agent_by_its_records_id` in full, in the style of `test_send_message.py` and `test_list_stop_agents.py`.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py -n 0 -p no:cacheprovider -q`
Expected: the six new tests fail (`retries` is not an argument; the tick starts the record on channel `task`; a claimed record with no session stays `running`).

- [ ] **Step 3: Implement as described under Interfaces and Behaviour**

The `retries` property:

```python
"retries": {
    "type": "integer", "minimum": 0, "maximum": 5,
    "description": "Optional number of times to start it again from its brief if it fails (default 0). Needs run_in_background=true.",
},
```

- [ ] **Step 4: Run the family's test files and the task layer's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/test_agent_tool.py tests/integration/test_send_message.py tests/integration/test_list_stop_agents.py tests/integration/tasks -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add `retries?` to section 13 and a paragraph saying an agent with it is named by one id across its attempts; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/harness/agent_records.py surogates/harness/subagents.py surogates/tasks/dispatcher.py tests/integration/test_agent_records.py docs/sub-agents/index.md
git commit -m "feat(agents): an agent with retries starts through its record, by the tool or by the tick"
```

---

### Task 4: Dependencies

**Files:**
- Modify: `surogates/tools/builtin/agents.py` (`_AGENT_SCHEMA`, `_agent_handler`)
- Modify: `surogates/harness/agent_records.py` (`adopt`, `waiting_on`, `brief_for`)
- Modify: `surogates/harness/subagents.py` (`tell_parent`), `surogates/harness/agent_news.py` (`notification_note`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_dependencies.py`

**Interfaces:**
- Produces, in `agent_records.py`:

```python
async def adopt(session_factory, session_store, session: Any) -> UUID: ...   # a record for a background agent that has none
async def waiting_on(session_factory, record_id: UUID) -> list[UUID]: ...    # the records still todo that name this one
```

- Produces: `agent` argument `after`: an array of at most 10 agent ids.
- Produces: an `agent.notification` with status `failed` or `killed` carries `waiting_on_it: [record ids]` when any record waits on the agent.

Behaviour:
- Each id in `after` must resolve to an agent the caller started in the background (`parent_session_id` is the caller, mode `background`). Otherwise the call fails with `no agent <id> that you started in the background` and creates nothing. A background agent without a record is adopted: `adopt` makes a record with `current_session_id` its session, `attempt_count` and `max_attempts` 1, and the status its session's state gives (`running`; `done` with its final message as `result`; `failed`; `cancelled` for a stopped one), sets the session's `task_id`, and writes `RECORD_KEY` into its dispatch.
- A named agent whose record is `failed` or `cancelled` is refused too, with `agent <id> failed: it cannot be waited for` (or `was stopped`): it would never release its dependent.
- The call returns `{"agent_id": <record id>, "status": "waiting_for_agents", "waiting_for": [...], "description": ..., "note": WAITING_NOTE}` when it waits, and starts at once (as in Task 3) when every dependency is already `done`.
- `WAITING_NOTE = "It starts once every agent it waits for has completed. If one of them fails or is stopped, it stays waiting and you are told: stop it or start a replacement."`
- `brief_for` puts the dependencies' reports after the prompt, each cut to `MAX_RESULT_CHARS`, in this shape:

```text
<prompt>

## Reports of the agents this one waited for

### <description> (<agent id>)
<report>
```

- `notification_note` adds a last line to a `failed` or `killed` agent's note when `waiting_on_it` is not empty: `[1 agent waits for it and will not start: stop it or start a replacement.]` (`N agents wait` for more).
- The tick's promote step needs no change: it already releases a `todo` row only when every parent is `done`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_agent_waits_for_another_and_starts_with_its_report(chats):
    chat = await a_chat(chats, PARITY)
    first = await a_started(chats, chat, "call_a", description="Gather the findings", prompt="Gather them.", retries=1)
    chats.replies = [
        calling("agent", "call_b", description="Summarise", prompt="Summarise the findings.",
                run_in_background=True, after=[first["agent_id"]]),
        _final_response("Queued."),
    ]
    await chats.says(chat, "Then summarise.")
    await chats.wake(chat)
    second = json.loads(await tool_result(chats, chat, "call_b"))
    assert second["status"] == "waiting_for_agents" and second["waiting_for"] == [first["agent_id"]]
    assert len(await children(chats, chat)) == 1

    chats.replies = [_final_response("Found three issues.")]
    await chats.wake((await children(chats, chat))[0].id)
    await tick(chats)   # settles the first, then starts the second
    started = [c for c in await children(chats, chat) if str(c.task_id) == second["agent_id"]]
    [brief] = await chats.store.get_events(started[0].id, types=[EventType.USER_MESSAGE])
    assert brief.data["content"] == (
        "Summarise the findings.\n\n## Reports of the agents this one waited for\n\n"
        f"### Gather the findings ({first['agent_id']})\nFound three issues."
    )


async def test_a_failed_dependency_keeps_its_dependents_waiting_and_says_so(chats):
    # first (no retries) fails; the tick runs; the second's record is still todo, no session of it exists.
    ...
    assert (await record_of(chats, UUID(second["agent_id"]))).status == "todo"
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["waiting_on_it"] == [second["agent_id"]]
    assert notification_note(note.data)["content"].endswith(
        "[1 agent waits for it and will not start: stop it or start a replacement.]")


async def test_after_names_only_agents_the_caller_started_in_the_background(chats):
    # an id of another chat's agent, a foreground agent's id, and a random uuid: each the same error, nothing created.
    ...
    assert "that you started in the background" in json.loads(await tool_result(chats, chat, "call_b"))["error"]


async def test_an_agent_that_already_failed_or_was_stopped_cannot_be_waited_for(chats):
    # the first agent (no retries) failed and the tick settled it; the second names it in after.
    ...
    assert json.loads(await tool_result(chats, chat, "call_b"))["error"] == (
        f"agent {first['agent_id']} failed: it cannot be waited for")


async def test_a_plain_background_agent_named_in_after_gets_a_record(chats):
    # a plain background agent (no retries) is started, then named in after.
    ...
    adopted = await record_of(chats, plain_child)
    assert (adopted.status, adopted.current_session_id, adopted.max_attempts) == ("running", plain_child.id, 1)


async def test_a_long_report_of_a_dependency_is_cut_in_the_brief(chats):
    # the first agent's final message is "x" * 12_000.
    ...
    assert brief.data["content"].endswith("x" * 10_000) and "x" * 10_001 not in brief.data["content"]
```

`a_started` comes from `test_agent_records.py`. Write the bodies marked `...` in full.

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

`after` takes the same two refusals as `retries` (Task 3).

- [ ] **Step 4: Run the new file and the family's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_dependencies.py tests/integration/test_agent_records.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Document `after?`, the status `waiting_for_agents` and `waiting_on_it` in section 13; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/harness/agent_records.py surogates/harness/subagents.py surogates/harness/agent_news.py tests/integration/test_agent_dependencies.py docs/sub-agents/index.md
git commit -m "feat(agents): an agent starts after the agents it names have completed, with their reports"
```

---

### Task 5: Agents over the limit wait for a slot

**Files:**
- Modify: `surogates/tools/builtin/agents.py` (`_start_agent`, `_agent_handler`)
- Modify: `surogates/tasks/dispatcher.py` (`_enqueue_ready_tasks`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_records.py`, `tests/integration/test_agent_tool.py`

**Interfaces:**
- Consumes: `create_record`, `claim`, `start_record`.
- Produces: a background call over `MAX_RUNNING_IN_BACKGROUND` returns `{"agent_id": <record id>, "status": "queued", "description": ..., "note": QUEUED_NOTE}`.
- `QUEUED_NOTE = "It starts when one of your running agents ends: 5 already run in the background."`

Behaviour:
- In the handler, a background call that finds the limit reached leaves its record `ready` and answers `queued`. A plain call gets a record for this (as a call with `retries=0` would have); a call with `after` or `retries` already has one. A `fork` over the limit is still refused with today's error.
- In `_enqueue_ready_tasks`, a record is claimable only when (a) its parent session's status is not `paused`, `failed` or `archived`, and (b) fewer than `MAX_RUNNING_IN_BACKGROUND` sessions with that parent are `active` or `processing` with dispatch mode `background`. The limit is read from `agents.MAX_RUNNING_IN_BACKGROUND` when the claim runs, so one constant rules the tool and the tick (the tests patch it). Old rows are claimed as today.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_sixth_background_agent_is_queued_and_starts_when_one_ends(chats):
    chat = await a_chat(chats, PARITY)
    for n in range(5):
        await a_started(chats, chat, f"call_{n}", description=f"Part {n}", prompt=f"Do part {n}.")
    sixth = await a_started(chats, chat, "call_5", description="Part 5", prompt="Do part 5.")
    assert sixth["status"] == "queued" and len(await children(chats, chat)) == 5
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

In `tests/integration/test_agent_tool.py`, `test_a_session_runs_at_most_five_agents_in_the_background` sets the limit to 2 and starts three: change it to expect `"status": "queued"` for the third and two children, and add a case that a third started with `fork=true` still gets the error `2 of your agents already run in the background`.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_records.py tests/integration/test_agent_tool.py -n 0 -p no:cacheprovider -q`
Expected: the new tests and the changed one fail with the "already run in the background" error.

- [ ] **Step 3: Implement the two behaviours**

- [ ] **Step 4: Run the same files and `tests/integration/test_send_message.py` (its resume-and-start test shares the limit)**

Expected: all pass.

- [ ] **Step 5: Document the status `queued` in section 13; commit**

```bash
git add surogates/tools/builtin/agents.py surogates/tasks/dispatcher.py tests/integration/test_agent_records.py tests/integration/test_agent_tool.py docs/sub-agents/index.md
git commit -m "feat(agents): a background agent over the limit waits for a slot"
```

---

### Task 6: Retries

**Files:**
- Modify: `surogates/harness/agent_records.py` (`classify_run`, `brief_for`, `tokens_left`)
- Modify: `surogates/tasks/dispatcher.py` (`_finalize_ended_sessions`)
- Modify: `surogates/tools/builtin/agents.py` (`start_record`)
- Modify: `surogates/harness/subagents.py` (`tell_parent`), `surogates/harness/agent_news.py`
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_retries.py`

**Interfaces:**
- Produces, in `agent_records.py`:

```python
class RunOutcome(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"      # used from Task 8 on
    FAILED = "failed"
    STOPPED = "stopped"

def run_of(events: list[Any]) -> list[Any]: ...                # the events of the session's last run
def classify_run(events: list[Any], session_status: str) -> RunOutcome: ...
async def tokens_left(session_factory, session_store, record: Task) -> int | None: ...   # None for no budget
```

Behaviour:
- `run_of` is the slice `loop_artifact_completion.py` takes for a resumed agent: the events after the `session.complete` before the last one.
- `classify_run`: session `completed` gives `COMPLETED`; `failed` or `cancelled` gives `FAILED`; `paused` gives `STOPPED`.
- In `_finalize_ended_sessions`, a row with `agent_dispatch` set is settled by `classify_run`: `COMPLETED` sets `done`, `result` (the run's final message, or `budget_report` for a budget stop) and `completed_at`; `FAILED` sets `ready` when `attempt_count < max_attempts`, else `failed` with `completed_at`; `STOPPED` sets `cancelled` with `completed_at`. It emits no `TASK_FAILED` for a record: the agent's own end tells the parent. Old rows are settled as today.
- `brief_for` adds, after the dependencies' reports, for a record with earlier attempts:

```text
## Earlier attempts

- Attempt 1 (failed): <its last words, cut to 500 characters, or "it ended without a report">
```

- The token budget covers all attempts. `create_record` stores the budget in `agent_dispatch["tokens"]` (the handler passes what `_agent_handler` computes as `child_tokens`). `tokens_left` is that minus `session_tree_tokens` of every earlier attempt's session. `start_record` gives the new attempt `tokens_left`; when it is 0 or less, the record is set `failed` and the parent gets a notification with status `budget`.
- `tell_parent`: for status `failed`, when the agent's record has `attempt_count < max_attempts`, it writes nothing (no notification, no `delegation.failed`): a retry follows. Otherwise its payload gains `"attempts": <attempt_count>` when that is over 1, and `notification_note` says `failed after 3 attempts: <error>`.

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
    assert await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION]) == []

    await tick(chats)   # back to ready, and started again
    second = next(c for c in await children(chats, chat) if c.id != first.id)
    record = await record_of(chats, UUID(started["agent_id"]))
    assert (record.status, record.attempt_count, record.current_session_id) == ("running", 2, second.id)
    [brief] = await chats.store.get_events(second.id, types=[EventType.USER_MESSAGE])
    assert brief.data["content"] == (
        "Check the totals.\n\n## Earlier attempts\n\n- Attempt 1 (failed): The file is locked."
    )

    await fail(chats, second, "provider error")
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert (note.data["agent_id"], note.data["status"], note.data["attempts"]) == (started["agent_id"], "failed", 2)
    assert "failed after 2 attempts: provider error" in notification_note(note.data)["content"]
    await tick(chats)
    assert (await record_of(chats, record.id)).status == "failed" and len(await children(chats, chat)) == 2


async def test_a_stopped_agent_is_not_started_again(chats):
    # retries=2; the chat stops it with stop_agent; the tick runs.
    ...
    assert (await record_of(chats, record.id)).status == "cancelled" and len(await children(chats, chat)) == 1


async def test_a_message_reaches_the_attempt_that_runs_now(chats):
    # after one failed attempt and a tick, send_message(to=<record id>) is written to the second attempt's log.
    ...
    assert await chats.store.get_events(second.id, types=[EventType.COORDINATOR_MESSAGE])


async def test_the_token_budget_covers_every_attempt(chats):
    # agent_token_budget 1000 on the chat; the first attempt's session used 600 tokens and failed.
    ...
    assert second.config[AGENT_BUDGET_KEY] == {"tokens": 400}
```

`fail(chats, session, error)` in this file ends a sub-agent as a failed turn does: `await chats.store.update_session_status(session.id, "failed")`, then `await tell_parent(store=chats.store, redis=chats.api.app.state.redis, session=await chats.session(session.id), status="failed", error=error)`. Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py -n 0 -p no:cacheprovider -q`
Expected: 4 failed (the first: a notification was written for the first failure).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file, the records' and the task layer's**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py tests/integration/test_agent_records.py tests/integration/test_agent_budget.py tests/integration/tasks -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add a "Retries" subsection to section 13 (fresh start, earlier attempts in the brief, one id, the budget over all attempts, a stop is not retried); commit**

```bash
git add surogates/harness/agent_records.py surogates/tasks/dispatcher.py surogates/tools/builtin/agents.py surogates/harness/subagents.py surogates/harness/agent_news.py tests/integration/test_agent_retries.py docs/sub-agents/index.md
git commit -m "feat(agents): a failed agent is started again from its brief, up to its retries"
```

---

### Task 7: A recorded result

**Files:**
- Modify: `surogates/session/events.py` (`AGENT_RESULT = "agent.result"`)
- Modify: `surogates/tools/builtin/agents.py` (new tool `record_result`, `register`)
- Modify: `surogates/tools/router.py` (`TOOL_LOCATIONS`: `"record_result": ToolLocation.HARNESS`)
- Modify: `surogates/harness/subagents.py` (`RESULT_TOOL`, `apply_parity`, `recorded_result`, `tell_parent`)
- Modify: `surogates/harness/agent_news.py`, `surogates/tools/builtin/delegate.py` (the foreground result), `surogates/tasks/dispatcher.py` (`result_metadata`)
- Modify: every list that names `list_agents` as one of the family's tools (find them with `git grep -n list_agents -- surogates`): `record_result` joins those that apply to a sub-agent's own tools.
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_results.py`

**Interfaces:**
- Produces: tool `record_result(data: object)`, offered to every session the `agent` tool started (it has `agent_dispatch`), and to no other.
- Produces, in `subagents.py`: `RESULT_TOOL = "record_result"`, `MAX_RECORDED_RESULT_CHARS = 20_000`, `def recorded_result(events: list[Any]) -> dict | None` (the `data` of the last `agent.result` event in `run_of(events)`).
- Produces: `agent.notification` payload key `metadata`; `tasks.result_metadata` for a record.

Behaviour:
- The handler writes an `agent.result` event `{"data": data}` on the caller's log and answers `{"status": "recorded"}`. A later call in the same run replaces the earlier one.
- Errors: the caller has no `agent_dispatch`: `only a sub-agent records a result`. `data` is not a JSON object: `data must be an object`. Its JSON is over 20,000 characters: `the result is too large: 20000 characters at most`.
- `tell_parent` adds `metadata` when `recorded_result` is not None. `notification_note` puts `[Recorded result: <json>]` on the line after the head. A foreground call's result gets `\n\n[Recorded result: <json>]` before its `[agent_id: …]`. The tick writes it to `result_metadata` when it settles a record as `done`, and clears it when the run recorded none.

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
    started = await a_started(chats, chat, "call_a", description="Verify the build", prompt="Run the checks.", retries=1)
    [child] = await children(chats, chat)
    chats.replies = [
        calling("record_result", "call_r", data={"tests_run": 12, "failed": 0}),
        _final_response("All twelve checks pass."),
    ]
    await chats.wake(child.id)
    assert "record_result" in chats.offered[-1]
    [note] = await chats.store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])
    assert note.data["metadata"] == {"tests_run": 12, "failed": 0}
    assert notification_note(note.data)["content"] == (
        f"[Agent {started['agent_id']} (Verify the build) completed]\n"
        '[Recorded result: {"tests_run": 12, "failed": 0}]\nAll twelve checks pass.'
    )
    await tick(chats)
    record = await record_of(chats, UUID(started["agent_id"]))
    assert (record.result, record.result_metadata) == ("All twelve checks pass.", {"tests_run": 12, "failed": 0})


async def test_a_foreground_agents_recorded_result_is_in_the_calls_result(chats):
    ...
    assert result.endswith(f'[Recorded result: {{"ok": true}}]\n\n[agent_id: {child.id}]')


@pytest.mark.parametrize("data, error", [
    ([1, 2], "data must be an object"),
    ({"blob": "x" * 20_001}, "the result is too large: 20000 characters at most"),
])
async def test_a_result_that_is_no_object_or_too_large_is_refused(chats, data, error):
    ...
    assert json.loads(await tool_result(chats, child.id, "call_r"))["error"] == error
    assert await chats.store.get_events(child.id, types=[EventType.AGENT_RESULT]) == []


async def test_a_chat_is_not_offered_the_tool_and_cannot_record(chats):
    chat = await a_chat(chats, PARITY)
    chats.replies = [calling("record_result", "call_r", data={"a": 1}), _final_response("No.")]
    await chats.says(chat, "Record it.")
    await chats.wake(chat)
    assert "record_result" not in chats.offered[-1]
    assert await chats.store.get_events(chat, types=[EventType.AGENT_RESULT]) == []


async def test_a_run_that_records_nothing_clears_what_an_earlier_run_recorded(chats):
    # the agent records {"a": 1} and completes; the chat messages it; its second run records nothing.
    ...
    assert "metadata" not in second_note.data
```

Add `"record_result"` to `REAL` in `tests/integration/test_agent_tool.py`, so the tool runs for real in these tests. Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q`
Expected: all fail (`record_result` is not offered; no `AGENT_RESULT`).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file and the family's, and the test that pins tool routing**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py tests/integration/test_agent_tool.py tests/integration/test_agent_retries.py $(git grep -l TOOL_LOCATIONS -- tests) -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add `record_result` to the tools table of section 13 and `metadata` to the notification's fields; commit**

```bash
git add surogates/session/events.py surogates/tools/builtin/agents.py surogates/tools/router.py surogates/harness/subagents.py surogates/harness/agent_news.py surogates/tools/builtin/delegate.py surogates/tasks/dispatcher.py tests/integration/test_agent_results.py tests/integration/test_agent_tool.py docs/sub-agents/index.md
git commit -m "feat(agents): a sub-agent records structured data with its result"
```

Add to that `git add` any file the `list_agents` search made you change.

---

### Task 8: Waiting for an answer

**Files:**
- Modify: `surogates/session/events.py` (`AGENT_WAITING = "agent.waiting"`)
- Modify: `surogates/tools/builtin/agents.py` (`_SEND_MESSAGE_SCHEMA`, `_send_message_handler`)
- Modify: `surogates/harness/subagents.py` (`asked_parent`, `tell_parent`), `surogates/harness/agent_news.py`
- Modify: `surogates/harness/agent_records.py` (`classify_run`, `reopen_waiting`), `surogates/tasks/dispatcher.py` (`_finalize_ended_sessions`)
- Modify: `surogates/tools/builtin/delegate.py` (the foreground result)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_results.py`

**Interfaces:**
- Produces: `send_message` argument `needs_reply` (boolean).
- Produces, in `subagents.py`: `def asked_parent(events: list[Any]) -> str | None`: the `summary` of the last `agent.waiting` event in `run_of(events)`, or None.
- Produces: notification status `waiting_for_answer`; `classify_run` returns `RunOutcome.WAITING` for a `completed` session whose run `asked_parent`.
- Produces, in `agent_records.py`: `async def reopen_waiting(session_factory, record_id: UUID) -> None` (`blocked` to `running`, `blocked_reason` cleared, `max_attempts` untouched).

Behaviour:
- `needs_reply=true` is valid only in a message to `"parent"` from a session with `agent_dispatch`. Otherwise the call fails with `needs_reply is for a message to the agent that started you`, and nothing is sent.
- With it, the handler also writes `agent.waiting` `{"summary": summary, "tool_call_id": ...}` on the sender's log, and its note is `Delivered. End your turn now: you resume when the answer comes.`
- When that run ends `completed`: a background agent's notification has status `waiting_for_answer` and its head is `[Agent <id> (<description>) is waiting for your answer: reply to it with send_message]`; no `delegation.complete` is written. A foreground call's result gets `\n\n[It is waiting for your answer: reply to it with send_message.]` before its `[agent_id: …]`.
- The tick settles such a record as `blocked` with `blocked_reason` the summary: it is not `done`, so its dependents stay `todo`, and `attempt_count` is not touched.
- When the parent's `send_message` resumes it (`_send_message_handler`, where `resumed` is true), a `blocked` record is put back to `running` with `reopen_waiting`.

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

    chats.replies = [
        calling("send_message", "call_answer", to=asker["agent_id"], message="The third quarter.", summary="Q3"),
        _final_response("Answered."),
    ]
    await chats.says(chat, "It is Q3.")
    await chats.wake(chat)
    assert json.loads(await tool_result(chats, chat, "call_answer"))["status"] == "resumed"
    assert (await record_of(chats, record.id)).status == "running"
    chats.replies = [_final_response("The memo is drafted.")]
    await chats.wake(child.id)
    await tick(chats)
    assert (await record_of(chats, record.id)).status == "done" and len(await children(chats, chat)) == 2


@pytest.mark.parametrize("to", ["sibling", "from a chat"])
async def test_needs_reply_is_only_for_a_sub_agents_message_to_its_parent(chats, to):
    ...
    assert json.loads(result)["error"] == "needs_reply is for a message to the agent that started you"


async def test_a_question_of_an_earlier_run_does_not_make_a_later_run_wait(chats):
    # the agent asked, was answered, and its second run ends without asking.
    ...
    assert notes[-1].data["status"] == "completed"


async def test_a_foreground_agent_that_asks_says_so_in_the_calls_result(chats):
    ...
    assert result.endswith(f"[It is waiting for your answer: reply to it with send_message.]\n\n[agent_id: {child.id}]")
```

Write the bodies marked `...` in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q -k "asks or needs_reply or earlier_run"`
Expected: all fail (`needs_reply` is not an argument).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the new file and the files of the messages and the dependencies**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_results.py tests/integration/test_send_message.py tests/integration/test_agent_dependencies.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Document `needs_reply?` and the status `waiting_for_answer` in section 13; commit**

```bash
git add surogates/session/events.py surogates/tools/builtin/agents.py surogates/harness/subagents.py surogates/harness/agent_news.py surogates/harness/agent_records.py surogates/tasks/dispatcher.py surogates/tools/builtin/delegate.py tests/integration/test_agent_results.py docs/sub-agents/index.md
git commit -m "feat(agents): a sub-agent that needs an answer waits for it, and its dependents with it"
```

---

### Task 9: An agent that runs again

**Files:**
- Modify: `surogates/harness/agent_records.py` (`reopen`)
- Modify: `surogates/tools/builtin/agents.py` (`_send_message_handler`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_agent_retries.py`

**Interfaces:**
- Consumes: `resolve`, `reopen_waiting` (Task 8).
- Produces, in `agent_records.py`: `async def reopen(session_factory, record_id: UUID) -> None`: a record that is `done`, `failed` or `cancelled` goes to `running`, `completed_at` is cleared, and `max_attempts` is set to `attempt_count`.

Behaviour:
- Where `_send_message_handler` resumes a resting agent that has a record, it calls `reopen` (or `reopen_waiting` for a `blocked` one). `_why_not_resume` keeps deciding who may resume what.
- Setting `max_attempts` to `attempt_count` is what makes a run resumed by a message end as `failed` with no retry: a fresh start from the brief would drop the follow-up.
- When the resumed run ends, the tick settles it as any run (Tasks 6 to 8), so its report and recorded result replace the earlier ones, and a dependency that now completes releases the records that waited on it. Records that had already started are not touched.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_finished_agent_asked_to_work_again_reports_anew_and_its_record_follows(chats):
    # retries=1; it completes with a recorded result {"failed": 2}; the tick runs; the chat messages it "check again";
    # its second run records {"failed": 0} and completes; the tick runs.
    ...
    assert (reopened.status, reopened.completed_at, reopened.max_attempts) == ("running", None, 1)
    assert (final.status, final.result, final.result_metadata) == ("done", "All pass now.", {"failed": 0})


async def test_a_resumed_run_that_fails_is_not_started_again(chats):
    # retries=2; it completes; the chat messages it; the resumed run fails.
    ...
    assert (record.status, record.attempt_count, len(await children(chats, chat))) == ("failed", 1, 1)
    assert notes[-1].data["status"] == "failed"


async def test_a_dependency_that_completes_after_a_resume_releases_what_waited(chats):
    # A (no retries) fails; B after A stays todo; the chat messages A, which resumes and completes; the tick runs.
    ...
    assert (await record_of(chats, UUID(b["agent_id"]))).status == "running"


async def test_a_dependent_that_already_runs_is_not_touched_by_a_resume(chats):
    # A completes; B after A runs; the chat messages A, which resumes.
    ...
    assert (await record_of(chats, UUID(b["agent_id"]))).status == "running" and b_session_status == "active"
```

Write the bodies in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py -n 0 -p no:cacheprovider -q -k "again or resume"`
Expected: 4 failed (the record stays `done` or `failed` after the resume).

- [ ] **Step 3: Implement `reopen` and the call in `_send_message_handler`**

- [ ] **Step 4: Run the retries', the messages' and the dependencies' files**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_agent_retries.py tests/integration/test_send_message.py tests/integration/test_agent_dependencies.py tests/integration/test_agent_results.py -n 0 -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 5: Add "An agent that runs again" to section 13 (the four rules of the spec's section of that name); commit**

```bash
git add surogates/harness/agent_records.py surogates/tools/builtin/agents.py tests/integration/test_agent_retries.py docs/sub-agents/index.md
git commit -m "feat(agents): a message that resumes an agent re-opens its record"
```

---

### Task 10: Stop and list agents that have not started

**Files:**
- Modify: `surogates/harness/agent_records.py` (`cancel_unstarted`, `records_of`)
- Modify: `surogates/tools/builtin/agents.py` (`_stop_agent_handler`, `_list_agents_handler`, `_send_message_handler`, `_LIST_AGENTS_SCHEMA`, `_STOP_AGENT_SCHEMA`)
- Modify: `docs/sub-agents/index.md`
- Test: `tests/integration/test_list_stop_agents.py`

**Interfaces:**
- Produces, in `agent_records.py`:

```python
async def cancel_unstarted(session_factory, record_id: UUID, parent_session_id: UUID) -> bool: ...
    # todo or ready -> cancelled, only for a record of that parent; False when it had started or is not its
async def records_of(session_factory, parent_session_id: UUID) -> list[Task]: ...   # the parent's records, oldest first
```

Behaviour:
- `stop_agent` with the id of a record that is `todo` or `ready` cancels it and answers `{"status": "stopped", "agent_id": <id>, "note": "It had not started: it will not start."}`. A cancelled record never starts, even when its dependencies complete (the promote step only touches `todo`).
- `list_agents` lists each of the caller's records once, under the record's id, with `status` from `RECORD_STATUS`, `description`, `mode: "background"`, and `waiting_for` (the ids of its dependencies that are not `done`) when it is `waiting_for_agents`. A started agent with a record is not listed a second time under its session's id. Records `done`, `failed` or `cancelled` that never had a session are listed too.
- `send_message` to a record that has not started fails with `agent <id> has not started yet: stop it and start it again with the new brief`.
- `_LIST_AGENTS_SCHEMA`'s description names the statuses: `running, completed, failed, stopped, queued, waiting_for_agents, waiting_for_answer`.

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


async def test_a_queued_agent_is_stopped_the_same_way(chats):  # noqa: F811
    ...


async def test_a_message_to_an_agent_that_has_not_started_is_refused(chats):  # noqa: F811
    ...
    assert "has not started yet" in json.loads(await tool_result(chats, chat, "call_message"))["error"]


async def test_an_agent_cannot_stop_another_sessions_waiting_agent(chats):  # noqa: F811
    ...
    assert "no agent" in json.loads(await tool_result(chats, other, "call_stop"))["error"]
```

Write the bodies in full.

- [ ] **Step 2: Run and see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/integration/test_list_stop_agents.py -n 0 -p no:cacheprovider -q -k "waits or queued or not_started or another_sessions"`
Expected: 4 failed (`no agent <id> that you started`).

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
2. With the flag off, the old behaviour is untouched: run `tests/integration/tasks`, `tests/test_delegate.py`, `tests/integration/test_workstream_threads.py` and `tests/integration/missions`. Expected: the same results as on `origin/master`.
3. One fresh reviewer reads the whole branch against the spec and this plan.
4. Open one pull request against `master`. It changes nothing on PROD, where the flag is off.
