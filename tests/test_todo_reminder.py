"""The reminder a session gets when its task list has gone stale."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from surogates.harness.todo_reminder import (
    TODO_REMINDER,
    TODO_REMINDER_KEY,
    TODO_REMINDER_MARKER,
    inject_todo_reminder,
    iterations_since_todo,
    stale_todo_reminder,
    strip_todo_reminders,
)


def call(name: str) -> dict:
    return {"id": "c1", "function": {"name": name, "arguments": "{}"}}


class Store:
    def __init__(self, snapshot=None, error: Exception | None = None):
        self.snapshot = snapshot
        self.error = error

    async def latest_todo_snapshot(self, session_id):
        if self.error is not None:
            raise self.error
        return self.snapshot


def item(status: str) -> dict:
    return {"id": status, "content": status, "status": status}


def test_a_todo_call_brings_the_count_to_zero_and_any_other_adds_one():
    assert iterations_since_todo(4, [call("read_file"), call("terminal")]) == 5
    assert iterations_since_todo(4, [call("read_file"), call("todo")]) == 0
    assert iterations_since_todo(0, []) == 1


async def test_a_list_with_an_item_still_open_is_reminded():
    for open_status in ("pending", "in_progress"):
        store = Store([item("completed"), item(open_status)])
        assert await stale_todo_reminder(store, uuid4()) == TODO_REMINDER


async def test_a_list_with_nothing_open_a_missing_list_and_a_failed_read_get_none():
    sid = uuid4()
    assert await stale_todo_reminder(Store([item("completed"), item("cancelled")]), sid) is None
    assert await stale_todo_reminder(Store([]), sid) is None
    assert await stale_todo_reminder(Store(None), sid) is None
    assert await stale_todo_reminder(Store(error=RuntimeError("db down")), sid) is None


def test_the_reminder_goes_in_the_last_result_as_a_key_for_json_and_a_line_for_text():
    first = {"role": "tool", "tool_call_id": "a", "content": "one"}
    last = {"role": "tool", "tool_call_id": "b", "content": json.dumps({"ok": True})}
    out = inject_todo_reminder([first, last], TODO_REMINDER)
    assert out[0] == {"role": "tool", "tool_call_id": "a", "content": "one"}
    assert json.loads(out[1]["content"]) == {"ok": True, TODO_REMINDER_KEY: TODO_REMINDER}

    plain = inject_todo_reminder([{"role": "tool", "tool_call_id": "a", "content": "done"}], TODO_REMINDER)
    assert plain[0]["content"] == f"done\n\n{TODO_REMINDER}"
    assert inject_todo_reminder([], TODO_REMINDER) == []


def test_replay_strips_the_reminder_from_both_shapes_and_leaves_the_rest():
    messages = [
        {"role": "tool", "content": json.dumps({"ok": True, TODO_REMINDER_KEY: TODO_REMINDER})},
        {"role": "tool", "content": f"done\n\n{TODO_REMINDER}"},
        {"role": "tool", "content": "untouched"},
        {"role": "assistant", "content": f"{TODO_REMINDER_MARKER} said by the model"},
    ]
    strip_todo_reminders(messages)
    assert json.loads(messages[0]["content"]) == {"ok": True}
    assert messages[1]["content"] == "done"
    assert messages[2]["content"] == "untouched"
    assert messages[3]["content"] == f"{TODO_REMINDER_MARKER} said by the model"
