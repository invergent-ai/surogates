"""What the old sub-agent mechanisms left behind still reads after they are gone.

A session's log may hold calls of the deleted tools, a plain worker's report and the task layer's events: replay
builds its conversation, the old calls ordinary tool calls and the task layer's events read as nothing.  The task layer's tables go with it, on a
database that has them and on one that never did.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from surogates.db.engine import apply_observability_ddl
from surogates.harness.loop_context_replay import replayed_messages
from surogates.session.models import Event

pytestmark = pytest.mark.asyncio(loop_scope="session")

OLD_CALLS = {"delegate_task": {"goal": "Count"}, "spawn_worker": {"goal": "Draft"}, "start_thread": {"title": "A", "goal": "A"}}


def _event(i: int, type_: str, data: dict) -> Event:
    return Event(id=i, session_id=uuid4(), type=type_, data=data, created_at=datetime.now(timezone.utc))


async def test_a_log_with_the_old_tools_and_their_events_replays():
    calls = [
        {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        for name, arguments in OLD_CALLS.items()
    ]
    events = [
        _event(1, "user.message", {"content": "Do the work."}),
        _event(2, "llm.response", {"message": {"role": "assistant", "content": "", "tool_calls": calls}}),
        *[
            _event(3 + i, "tool.result", {"tool_call_id": call["id"], "name": call["function"]["name"], "content": "{}"})
            for i, call in enumerate(calls)
        ],
        _event(6, "llm.response", {"message": {"role": "assistant", "content": "Started."}}),
        _event(7, "worker.complete", {"worker_id": "w1", "result": "Found three."}),
        _event(8, "task.blocked", {"task_id": "t1", "reason": "needs a key"}),
        _event(9, "user.message", {"content": "And now?"}),
    ]
    messages = replayed_messages(events, None)
    assert [m["role"] for m in messages][:2] == ["user", "assistant"]
    assert {c["function"]["name"] for c in messages[1]["tool_calls"]} == set(OLD_CALLS)
    # The task layer's events are read as nothing: no note of a task the session can no longer act on.
    assert not any("Task t1" in str(m.get("content", "")) for m in messages)
    assert messages[-1] == {"role": "user", "content": "And now?"}


async def test_a_masters_log_with_a_proposal_and_a_thread_started_from_its_card_replays():
    call = {"id": "call_p", "type": "function", "function": {"name": "agent", "arguments": json.dumps(
        {"description": "Draft A", "prompt": "Draft A.", "propose": True, "where": "cloud"},
    )}}
    proposed = {"status": "proposed", "proposal_id": "p1", "threads": [{"key": "1", "title": "Draft A"}]}
    events = [
        _event(1, "user.message", {"content": "Plan the report."}),
        _event(2, "llm.response", {"message": {"role": "assistant", "content": "", "tool_calls": [call]}}),
        _event(3, "tool.result", {"tool_call_id": "call_p", "name": "agent", "content": json.dumps(proposed)}),
        _event(4, "thread.proposed", {"proposal_id": "p1", "threads": [
            {"key": "1", "title": "Draft A", "goal": "Draft A.", "where": "cloud"},
        ]}),
        _event(5, "llm.response", {"message": {"role": "assistant", "content": "Start it from its card."}}),
        _event(6, "worker.spawned", {
            "worker_id": "t1", "title": "Draft A", "goal": "Draft A.", "started_by": "user", "proposal_id": "p1", "key": "1",
            "format": "task-notification",
        }),
        _event(7, "user.message", {"content": "And now?"}),
    ]
    messages = replayed_messages(events, None)
    # The old call stays an ordinary call; the proposal and the start from its card read as nothing.
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert not any("Draft A" in str(m.get("content", "")) for m in messages if m["role"] == "user")
    assert messages[-1] == {"role": "user", "content": "And now?"}


async def _exists(engine, query: str) -> bool:
    async with engine.connect() as conn:
        return bool((await conn.execute(text(query))).scalar())


async def test_the_schema_has_no_task_tables_whether_or_not_it_had_them(engine):
    tables = "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('tasks', 'task_links')"
    column = "SELECT count(*) FROM information_schema.columns WHERE table_name = 'sessions' AND column_name = 'task_id'"
    # A database the task layer once ran on.
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS tasks (id uuid PRIMARY KEY)"))
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS task_links (parent_id uuid NOT NULL REFERENCES tasks(id), "
            "child_id uuid NOT NULL REFERENCES tasks(id), PRIMARY KEY (parent_id, child_id))"
        ))
        await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS task_id uuid REFERENCES tasks(id)"))
    for _ in range(2):  # and again: every statement is idempotent
        async with engine.begin() as conn:
            await apply_observability_ddl(conn)
        assert not await _exists(engine, tables)
        assert not await _exists(engine, column)
