"""The todo list must survive a wake boundary.

`TodoStore` lived in a module-global dict that nothing persisted and nothing
evicted, so a new worker/pod/wake started blank: a read returned
`{"todos": [], "total": 0}` while the transcript plainly showed a list, and
`merge=true` fell through to replace (merging onto an empty map), silently
dropping everything written earlier.

The list is recovered from the last `todo.updated` event — every todo response
is already a complete snapshot, so recovery reads one row.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.todo import _todo_handler


class _Store:
    """Records emitted events and serves the latest todo snapshot from them."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, str, dict]] = []

    async def emit_event(self, session_id, type_, data) -> int:
        self.events.append((session_id, getattr(type_, "value", type_), data))
        return len(self.events)

    async def latest_todo_snapshot(self, session_id) -> list | None:
        for sid, type_, data in reversed(self.events):
            if sid == session_id and type_ == EventType.TODO_UPDATED.value:
                return data["todos"]
        return None


async def _call(store, sid, **args) -> dict:
    return json.loads(
        await _todo_handler(args, session_id=str(sid), session_store=store)
    )


def _new_wake() -> None:
    """Simulate a fresh worker: drop any in-process todo state.

    Without this the module-global store carries the list between calls and
    the wake-boundary tests pass for the wrong reason. A no-op once the
    global is gone, which is the point.
    """
    import surogates.tools.builtin.todo as todo_mod

    getattr(todo_mod, "_session_stores", {}).clear()


@pytest.mark.asyncio
async def test_merge_across_a_wake_boundary_keeps_earlier_items():
    """The bug: wake 2 merged onto nothing and dropped wake 1's plan."""
    store, sid = _Store(), uuid4()

    await _call(store, sid, todos=[
        {"id": "1", "content": "first", "status": "completed"},
        {"id": "2", "content": "second", "status": "pending"},
    ])

    _new_wake()  # Wake 2 — no shared process state, only the event log.
    out = await _call(store, sid, todos=[
        {"id": "3", "content": "third", "status": "pending"},
    ], merge=True)

    assert [t["id"] for t in out["todos"]] == ["1", "2", "3"]
    assert out["summary"]["total"] == 3


@pytest.mark.asyncio
async def test_a_cold_read_returns_the_list():
    store, sid = _Store(), uuid4()
    await _call(store, sid, todos=[{"id": "1", "content": "a", "status": "pending"}])

    _new_wake()
    out = await _call(store, sid)  # read
    assert [t["id"] for t in out["todos"]] == ["1"]


@pytest.mark.asyncio
async def test_a_session_that_never_wrote_reads_empty():
    out = await _call(_Store(), uuid4())
    assert out["todos"] == []
    assert out["summary"]["total"] == 0


@pytest.mark.asyncio
async def test_an_explicitly_emptied_list_stays_empty():
    """`[]` (emptied) must not be confused with `None` (never written)."""
    store, sid = _Store(), uuid4()
    await _call(store, sid, todos=[{"id": "1", "content": "a", "status": "pending"}])
    await _call(store, sid, todos=[])

    _new_wake()
    assert await _call(store, sid) == {
        "todos": [],
        "summary": {"total": 0, "pending": 0, "in_progress": 0,
                    "completed": 0, "cancelled": 0},
    }


@pytest.mark.asyncio
async def test_replace_still_replaces():
    store, sid = _Store(), uuid4()
    await _call(store, sid, todos=[{"id": "1", "content": "a", "status": "pending"}])
    _new_wake()
    out = await _call(store, sid, todos=[{"id": "9", "content": "z", "status": "pending"}])
    assert [t["id"] for t in out["todos"]] == ["9"]


@pytest.mark.asyncio
async def test_the_event_fires_on_write_only():
    store, sid = _Store(), uuid4()
    await _call(store, sid, todos=[{"id": "1", "content": "a", "status": "pending"}])
    await _call(store, sid)  # read
    await _call(store, sid)  # read

    todo_events = [e for e in store.events if e[1] == EventType.TODO_UPDATED.value]
    assert len(todo_events) == 1
    assert todo_events[0][2]["todos"][0]["id"] == "1"


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_a_list():
    store, a, b = _Store(), uuid4(), uuid4()
    await _call(store, a, todos=[{"id": "a1", "content": "a", "status": "pending"}])

    _new_wake()
    assert await _call(store, b) == {
        "todos": [],
        "summary": {"total": 0, "pending": 0, "in_progress": 0,
                    "completed": 0, "cancelled": 0},
    }


@pytest.mark.asyncio
async def test_no_store_still_answers():
    """Tool kwargs are best-effort; a missing store must not break the wake."""
    out = json.loads(
        await _todo_handler(
            {"todos": [{"id": "1", "content": "a", "status": "pending"}]},
            session_id=str(uuid4()),
        )
    )
    assert [t["id"] for t in out["todos"]] == ["1"]


@pytest.mark.asyncio
async def test_a_snapshot_read_failure_does_not_break_the_call():
    """A DB blip must degrade to a blank list, not raise into the tool loop."""
    class _Broken(_Store):
        async def latest_todo_snapshot(self, session_id):
            raise RuntimeError("db blip")

    out = await _call(_Broken(), uuid4())
    assert out["todos"] == []


def test_todo_is_not_dispatched_mid_stream():
    """A todo write allocates durable state, so a discarded stream must not
    have already committed it -- the rule PARALLEL_TOOLS' docstring states."""
    from surogates.harness.tool_exec import (
        BATCH_PARALLEL_TOOLS,
        PARALLEL_TOOLS,
        SAGA_EXCLUDED_TOOLS,
    )

    assert "todo" not in PARALLEL_TOOLS
    assert "todo" in BATCH_PARALLEL_TOOLS  # still parallel once committed
    assert "todo" not in SAGA_EXCLUDED_TOOLS
