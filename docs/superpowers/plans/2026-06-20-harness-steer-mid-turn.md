# Steer the Agent Mid-Turn Without Stopping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user message that arrives while the agent is mid-generation be queued and folded into the running loop at the next iteration boundary (Claude-Code-style steering), instead of dropping the in-flight response and restarting.

**Architecture:** Approach B from the design — keep `send_message` and the durable event log exactly as they are; fix everything inside the harness. A boundary injector in `_run_loop` pulls new `user.message` events past a per-wake steer cursor, coalesces them into one user turn, and continues the same wake. The staleness guard stops dropping responses (it aborts only on an explicit Stop/pause/lease-loss). A replay re-sequencer in `_rebuild_messages` defers a `user.message` that landed mid-iteration to that iteration's close, so a later rebuild reproduces the live order and never splits a tool-call / tool-result pair.

**Tech Stack:** Python 3.12, asyncio, pytest (`asyncio_mode = "auto"`), SQLAlchemy event store.

## Progress

- [x] Task 1: `coalesce_user_messages` helper
- [ ] Task 2: Replay re-sequencer in `_rebuild_messages`
- [ ] Task 3: `_collect_steer_messages` harness helper
- [ ] Task 4: Stop dropping the buffered response in the staleness guard
- [ ] Task 5: Boundary injector, steer cursor, turn-metadata reset (`loop.py` + `llm_call.py`)
- [ ] Task 6: Drain a follow-up at completion instead of completing + re-waking
- [ ] Task 7: Full-suite regression sweep

## Global Constraints

- **Repo:** `/work/surogates`. Default branch `master`. Work on branch `feat/harness-steer-mid-turn` (already created).
- **Commits:** Conventional Commits (`type(scope): subject`). **No** `Co-Authored-By` trailer. **Never** reference Plan/Task/Phase/Step numbers in code comments or commit messages.
- **No legacy fallbacks:** when changing the staleness guard, delete the old drop path entirely — do not keep both behind a branch.
- **Run tests with:** `pytest <path>` from `/work/surogates` (editable install; no `uv run` needed). `asyncio_mode = "auto"` — `async def test_*` works without a marker, but match the surrounding file's style.
- **Event ordering invariant:** every `assistant` message that carries `tool_calls` must be immediately followed by its corresponding `tool` (tool-result) messages in the rebuilt list. The re-sequencer must never violate this.

---

## File / responsibility map

| File | Responsibility | Change |
|---|---|---|
| `surogates/harness/loop_context_replay.py` | Message replay + the user-message renderer | Add `coalesce_user_messages`; re-sequence mid-iteration `user.message` in `_rebuild_messages` |
| `surogates/harness/loop.py` | The wake loop (`_run_loop`), the staleness guard | Add `_collect_steer_messages`; simplify the staleness guard; wire the boundary injector, steer cursor, turn-metadata reset, and completion drain |
| `surogates/harness/llm_call.py` | Streaming LLM event metadata | Let callers pass a turn-local `iteration_index` override so `llm.delta` retry/stream events match the active steered turn |
| `tests/test_steer_coalesce.py` | New | Unit tests for `coalesce_user_messages` |
| `tests/test_steer_replay_resequence.py` | New | Unit tests for the `_rebuild_messages` re-sequencer |
| `tests/test_steer_collect.py` | New | Unit tests for `_collect_steer_messages` |
| `tests/test_steer_turn_meta.py` | New | Unit tests for turn-local metadata stamping in `llm_call.py` |
| `tests/test_steer_loop.py` | New | Loop-level tests for injection, completion drain, turn-metadata, Stop precedence |

---

## Task 1: `coalesce_user_messages` helper

Pure function that merges one or more already-rendered user-message dicts into a single user turn. Used by both the live injector and the replay re-sequencer so the two paths produce identical bytes.

**Files:**
- Modify: `surogates/harness/loop_context_replay.py` (add a module-level function after `build_user_message_dict`, around line 82)
- Test: `tests/test_steer_coalesce.py` (create)

**Interfaces:**
- Produces: `coalesce_user_messages(messages: list[dict]) -> dict` — input is a non-empty list of `{"role": "user", "content": str | list[block]}` dicts; returns one `{"role": "user", "content": ...}`. Text-only inputs join with a blank line; if any input has list (multimodal) content, the result is a single block list preserving every block in order.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_steer_coalesce.py`:

```python
"""coalesce_user_messages merges queued steer messages into one user turn."""
from __future__ import annotations

from surogates.harness.loop_context_replay import coalesce_user_messages


def test_single_message_returned_unchanged():
    msg = {"role": "user", "content": "hello"}
    assert coalesce_user_messages([msg]) == msg


def test_two_text_messages_join_with_blank_line():
    out = coalesce_user_messages([
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ])
    assert out == {"role": "user", "content": "first\n\nsecond"}


def test_multimodal_message_produces_block_list_in_order():
    img_block = {"type": "image_url", "image_url": {"url": "data:x", "detail": "auto"}}
    out = coalesce_user_messages([
        {"role": "user", "content": "look at this"},
        {"role": "user", "content": [{"type": "text", "text": "and this"}, img_block]},
    ])
    assert out["role"] == "user"
    assert out["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": "and this"},
        img_block,
    ]


def test_empty_text_messages_are_skipped_in_join():
    out = coalesce_user_messages([
        {"role": "user", "content": "kept"},
        {"role": "user", "content": ""},
    ])
    assert out == {"role": "user", "content": "kept"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_steer_coalesce.py -v`
Expected: FAIL with `ImportError: cannot import name 'coalesce_user_messages'`

- [ ] **Step 3: Implement the helper**

In `surogates/harness/loop_context_replay.py`, add directly after the `build_user_message_dict` function (after line 81, before `class ContextReplayMixin`):

```python
def coalesce_user_messages(messages: list[dict]) -> dict:
    """Merge one or more rendered user-message dicts into a single user turn.

    Both the live boundary injector and the replay re-sequencer pass the
    same rendered messages here so a steered turn looks byte-identical
    whether it was injected live or reconstructed from the event log.

    Text-only messages join with a blank-line separator. If any message
    is multimodal (its ``content`` is a block list), the result is a
    single block list preserving every text and image block in order.
    """
    if len(messages) == 1:
        return messages[0]

    if any(isinstance(m.get("content"), list) for m in messages):
        blocks: list[dict] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                blocks.extend(content)
            elif content:
                blocks.append({"type": "text", "text": content})
        return {"role": "user", "content": blocks}

    text = "\n\n".join(m.get("content") or "" for m in messages if m.get("content"))
    return {"role": "user", "content": text}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_steer_coalesce.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop_context_replay.py tests/test_steer_coalesce.py
git commit -m "feat(harness): add coalesce_user_messages helper for steer turns"
```

---

## Task 2: Replay re-sequencer in `_rebuild_messages`

The durable log can hold a `user.message` physically between an `assistant(tool_calls)` and its `tool.result` (because the API writes it the instant it arrives). On rebuild, defer such a message to the iteration's close so the reconstructed order matches the live boundary-injection order and tool-call/tool-result adjacency is preserved.

**Files:**
- Modify: `surogates/harness/loop_context_replay.py` — `ContextReplayMixin._rebuild_messages` (lines 140-244)
- Test: `tests/test_steer_replay_resequence.py` (create)

**Interfaces:**
- Consumes: `coalesce_user_messages` (Task 1), `build_user_message_dict` (existing), `EventType`.
- Produces: no signature change to `_rebuild_messages(self, events: list[Event]) -> list[dict]`; only its ordering behavior changes for mid-iteration user messages.

Iteration state, tracked while folding events in id order:
- An iteration **opens** on `LLM_REQUEST`.
- A non-synthetic `USER_MESSAGE` seen while open is **buffered**, not appended.
- On `LLM_RESPONSE`: append the assistant message; if it has tool-call ids, stay open and remember them; if it has none, close and flush the buffer.
- On each `TOOL_RESULT`: append it and drop its id from the expected set; when the set empties, close and flush.
- `CONTEXT_COMPACT` resets messages and clears the buffer/open state (the compacted snapshot already contains any earlier steer turn).
- After the loop, flush any buffer left open by a log that ends mid-iteration.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_steer_replay_resequence.py`:

```python
"""_rebuild_messages re-sequences a mid-iteration user.message to the boundary."""
from __future__ import annotations

from types import SimpleNamespace

from surogates.session.events import EventType


def _event(etype: EventType, data: dict, eid: int):
    return SimpleNamespace(type=etype.value, data=data, id=eid)


def _rebuild(events):
    from surogates.harness.loop_context_replay import ContextReplayMixin

    host = type("_H", (ContextReplayMixin,), {})()
    return host._rebuild_messages(events)


def _assistant_with_tool_call(tc_id: str):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tc_id,
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }


def test_user_message_between_toolcall_and_result_moves_to_iteration_close():
    # llm.request, assistant(tool_call X), user(steer), tool.result(X)
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "also check Z"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    roles = [(m["role"], m.get("tool_call_id")) for m in _rebuild(events)]
    # assistant(tool_calls) MUST be immediately followed by its tool result,
    # and the steer message lands AFTER the tool result.
    assert roles == [
        ("user", None),       # "do it"
        ("assistant", None),  # tool_call X
        ("tool", "X"),        # result for X
        ("user", None),       # "also check Z" — deferred to the close
    ]
    assert _rebuild(events)[-1]["content"] == "also check Z"


def test_user_message_mid_stream_before_response_defers_past_tool_result():
    # user arrives between llm.request and llm.response of a tool-calling iter
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 3),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "tool", "user"]


def test_user_message_at_clean_boundary_keeps_natural_placement():
    # final response has no tool calls; next user message is a normal new turn
    events = [
        _event(EventType.USER_MESSAGE, {"content": "q1"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "a1"}}, 3),
        _event(EventType.USER_MESSAGE, {"content": "q2"}, 4),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "user"]


def test_two_steer_messages_in_one_window_are_coalesced():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "first"}, 4),
        _event(EventType.USER_MESSAGE, {"content": "second"}, 5),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 6),
    ]
    msgs = _rebuild(events)
    assert msgs[-1] == {"role": "user", "content": "first\n\nsecond"}
    # exactly one trailing user message (coalesced), not two
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]


def test_multi_tool_iteration_flushes_only_after_last_result():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "A", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                {"id": "B", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ],
        }}, 3),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "A", "content": "ok"}, 5),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "B", "content": "ok"}, 6),
    ]
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "tool", "tool", "user"]


def test_synthetic_user_message_is_not_deferred():
    # a synthetic nudge keeps its natural placement even mid-iteration
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "nudge", "synthetic": "x"}, 4),
        _event(EventType.TOOL_RESULT, {"tool_call_id": "X", "content": "ok"}, 5),
    ]
    # synthetic appended inline -> lands between assistant and tool result.
    # (Synthetic nudges are only ever emitted at real boundaries in practice;
    # this asserts we do not change their handling.)
    roles = [m["role"] for m in _rebuild(events)]
    assert roles == ["user", "assistant", "user", "tool"]


def test_log_ending_mid_iteration_flushes_buffer():
    events = [
        _event(EventType.USER_MESSAGE, {"content": "do it"}, 1),
        _event(EventType.LLM_REQUEST, {"iteration": 1}, 2),
        _event(EventType.LLM_RESPONSE, {"message": _assistant_with_tool_call("X")}, 3),
        _event(EventType.USER_MESSAGE, {"content": "steer"}, 4),
        # no tool.result yet — wake interrupted
    ]
    # buffer must still flush so the steer message is not lost
    assert _rebuild(events)[-1]["content"] == "steer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_steer_replay_resequence.py -v`
Expected: FAIL — current `_rebuild_messages` appends user messages inline, so e.g. `test_user_message_between_toolcall_and_result_moves_to_iteration_close` shows order `user, assistant, user, tool` instead of `user, assistant, tool, user`.

- [ ] **Step 3: Implement the re-sequencer**

Replace the body of `_rebuild_messages` (`surogates/harness/loop_context_replay.py:140-244`). Keep every existing branch; add the iteration-open state, defer non-synthetic `USER_MESSAGE` while open, handle `LLM_REQUEST`, and flush on iteration close. The full new body:

```python
    def _rebuild_messages(self, events: list[Event]) -> list[dict]:
        """Replay event log to reconstruct conversation messages.

        Processes events in order.  A ``CONTEXT_COMPACT`` event replaces
        all previously accumulated messages with the compacted set stored
        in its data payload.

        ``LLM_THINKING`` events are **skipped** during replay -- they are
        informational only and should not re-enter the conversation.

        ``LLM_DELTA`` events are likewise skipped; the full response is
        captured in the subsequent ``LLM_RESPONSE`` event.

        Mid-turn steering: a real ``user.message`` can land in the log
        while an LLM iteration is still open (mid-stream, or while its
        tool calls are running), because the API appends it the instant
        it arrives.  Such a message is deferred to the iteration's close
        and coalesced, so the rebuilt order matches the live
        boundary-injection order and never splits a tool-call / tool
        result pair.  ``tool.result`` events carry no iteration marker,
        so an open tool-calling iteration is closed by tracking the
        ``tool_calls[*].id`` set from its ``llm.response`` until every id
        has a matching result.
        """
        messages: list[dict] = []
        iteration_open = False
        awaiting_tool_ids: set[str] = set()
        deferred_users: list[dict] = []

        def _flush_deferred() -> None:
            nonlocal deferred_users
            if deferred_users:
                messages.append(coalesce_user_messages(deferred_users))
                deferred_users = []

        for event in events:
            etype = event.type

            if etype == EventType.LLM_REQUEST.value:
                iteration_open = True
                awaiting_tool_ids = set()

            elif etype == EventType.USER_MESSAGE.value:
                rendered = build_user_message_dict(event.data)
                if iteration_open and not (event.data or {}).get("synthetic"):
                    deferred_users.append(rendered)
                else:
                    messages.append(rendered)

            elif etype == EventType.LLM_RESPONSE.value:
                stored_message = event.data.get("message")
                if stored_message is not None:
                    messages.append(stored_message)
                tool_calls = (stored_message or {}).get("tool_calls") or []
                ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
                if ids:
                    awaiting_tool_ids = ids
                else:
                    iteration_open = False
                    awaiting_tool_ids = set()
                    _flush_deferred()

            elif etype == EventType.TOOL_RESULT.value:
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": event.data.get("content", ""),
                })
                if awaiting_tool_ids:
                    awaiting_tool_ids.discard(event.data.get("tool_call_id"))
                    if not awaiting_tool_ids:
                        iteration_open = False
                        _flush_deferred()

            elif etype == EventType.ADVISOR_RESULT.value and event.data.get("content"):
                messages.append({
                    "role": "user",
                    "content": self._format_advisor_context(
                        category=event.data.get("category", "advisor"),
                        content=str(event.data.get("content") or ""),
                    ),
                })

            elif (
                etype == EventType.BOARD_UPDATE.value
                and event.data.get("content")
            ):
                messages.append({
                    "role": "user",
                    "content": str(event.data["content"]),
                })

            elif etype == EventType.CONTEXT_COMPACT.value:
                compacted = event.data.get("compacted_messages")
                if compacted is not None:
                    messages = list(compacted)
                    # The compacted snapshot already contains any earlier
                    # steered turn in its proper place; drop the buffer and
                    # close the window so it is not re-appended.
                    deferred_users = []
                    iteration_open = False
                    awaiting_tool_ids = set()

            elif etype == EventType.WORKER_COMPLETE.value:
                worker_id = event.data.get("worker_id", "?")
                result = event.data.get("result", "")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} completed]\n{result}",
                })

            elif etype == EventType.WORKER_FAILED.value:
                worker_id = event.data.get("worker_id", "?")
                error = event.data.get("error", "unknown error")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} failed: {error}]",
                })

            elif etype == EventType.CODE_RUN_RESULT.value:
                agent = event.data.get("agent", "coding agent")
                if event.data.get("error"):
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} failed: {event.data['error']}]",
                    })
                else:
                    final = event.data.get("final_message", "")
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} finished]\n{final}",
                    })

            # LLM_THINKING and LLM_DELTA are intentionally skipped.

        # Flush any users deferred by an iteration that never closed (the
        # log ends mid-tool-execution because this is an in-progress wake).
        _flush_deferred()

        # Strip stale budget warnings from replayed tool results.
        strip_budget_warnings(messages)

        # Repair histories poisoned by a prior identical-call loop.
        return collapse_repeated_tool_rounds(messages)
```

Then add `coalesce_user_messages` to the imports already present in this module — it is defined in the same file above, so no import is needed; confirm the function is defined before the class (it is, from Task 1).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_steer_replay_resequence.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Run the existing replay tests to verify no regression**

Run: `pytest tests/test_board_replay_hydration.py tests/integration/test_attachment_history_replay.py -v`
Expected: PASS (existing behavior unchanged — those logs have no mid-iteration user messages)

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop_context_replay.py tests/test_steer_replay_resequence.py
git commit -m "feat(harness): re-sequence mid-iteration user messages on replay"
```

---

## Task 3: `_collect_steer_messages` harness helper

Async helper on `AgentHarness` that the live loop calls at a boundary: read non-synthetic `user.message` events past the steer cursor, render and coalesce them, and report the advanced cursor.

**Files:**
- Modify: `surogates/harness/loop.py` — add the method near `_has_stranded_user_message` (after line 662); add an import
- Test: `tests/test_steer_collect.py` (create)

**Interfaces:**
- Consumes: `self._store.get_events(session_id, *, after, types)` returning a list of objects with `.id` and `.data`; `EventType.USER_MESSAGE`; `build_user_message_dict`, `coalesce_user_messages` (Task 1).
- Produces: `async def _collect_steer_messages(self, session_id: UUID, after_event_id: int) -> tuple[dict | None, int]` — returns `(coalesced_user_message_or_None, new_cursor)`. The cursor advances to the max event id seen even when every message was synthetic, so synthetic events are never re-examined. Returns `(None, after_event_id)` when no events at all are past the cursor.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_steer_collect.py`:

```python
"""_collect_steer_messages pulls + coalesces post-cursor user messages."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from surogates.harness.loop import AgentHarness


def _event(eid: int, data: dict):
    return SimpleNamespace(id=eid, data=data)


def _harness(events):
    harness = AgentHarness.__new__(AgentHarness)
    store = AsyncMock()
    store.get_events = AsyncMock(return_value=events)
    harness._store = store
    return harness, store


async def test_no_new_events_returns_none_and_same_cursor():
    harness, _ = _harness([])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg is None
    assert cursor == 10


async def test_single_user_message_is_rendered_and_cursor_advances():
    harness, store = _harness([_event(11, {"content": "steer me"})])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "steer me"}
    assert cursor == 11
    # queried only USER_MESSAGE events past the cursor
    _, kwargs = store.get_events.await_args
    assert kwargs["after"] == 10


async def test_multiple_messages_coalesced_into_one_turn():
    harness, _ = _harness([
        _event(11, {"content": "first"}),
        _event(12, {"content": "second"}),
    ])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "first\n\nsecond"}
    assert cursor == 12


async def test_synthetic_messages_skipped_but_cursor_advances():
    harness, _ = _harness([
        _event(11, {"content": "nudge", "synthetic": "x"}),
        _event(12, {"content": "real"}),
    ])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg == {"role": "user", "content": "real"}
    assert cursor == 12


async def test_all_synthetic_returns_none_but_advances_cursor():
    harness, _ = _harness([_event(11, {"content": "nudge", "synthetic": "x"})])
    msg, cursor = await harness._collect_steer_messages(uuid4(), 10)
    assert msg is None
    assert cursor == 11
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_steer_collect.py -v`
Expected: FAIL with `AttributeError: 'AgentHarness' object has no attribute '_collect_steer_messages'`

- [ ] **Step 3: Add the import**

In `surogates/harness/loop.py`, find the existing import of `build_user_message_dict` (it may already be imported from `loop_context_replay`). If `coalesce_user_messages` is not yet imported, extend that import. Search first:

Run: `grep -n "build_user_message_dict\|from surogates.harness.loop_context_replay" surogates/harness/loop.py`

If there is a line like `from surogates.harness.loop_context_replay import build_user_message_dict`, change it to:

```python
from surogates.harness.loop_context_replay import (
    build_user_message_dict,
    coalesce_user_messages,
)
```

If `build_user_message_dict` is not imported there (the class uses it via the mixin module), add this import alongside the other `surogates.harness.loop_context_replay` / harness imports near the top of the file:

```python
from surogates.harness.loop_context_replay import (
    build_user_message_dict,
    coalesce_user_messages,
)
```

- [ ] **Step 4: Implement the helper**

In `surogates/harness/loop.py`, add immediately after `_has_stranded_user_message` (after line 662):

```python
    async def _collect_steer_messages(
        self,
        session_id: UUID,
        after_event_id: int,
    ) -> tuple[dict | None, int]:
        """Pull user messages that arrived past the steer cursor.

        Reads non-synthetic ``user.message`` events appended after
        ``after_event_id``, renders each through the same path replay uses
        (:func:`build_user_message_dict`), and coalesces them into one user
        turn so a burst of follow-ups becomes a single steered turn.

        Returns ``(coalesced_message_or_None, new_cursor)``.  The cursor
        advances to the highest event id seen even when every message was
        synthetic, so synthetic events (mission continuations, harness
        nudges) are never re-examined and never steer.
        """
        events = await self._store.get_events(
            session_id,
            after=after_event_id,
            types=[EventType.USER_MESSAGE],
        )
        if not events:
            return None, after_event_id
        new_cursor = max(event.id for event in events)
        rendered = [
            build_user_message_dict(event.data)
            for event in events
            if not (event.data or {}).get("synthetic")
        ]
        if not rendered:
            return None, new_cursor
        return coalesce_user_messages(rendered), new_cursor
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_steer_collect.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py tests/test_steer_collect.py
git commit -m "feat(harness): add _collect_steer_messages boundary helper"
```

---

## Task 4: Stop dropping the buffered response in the staleness guard

The guard currently aborts both on an explicit interrupt **and** on "a newer user.message exists." Remove the second cause — a mid-stream user message must no longer discard the response. Keep the explicit-interrupt abort (Stop / pause / lease loss) exactly as-is.

**Files:**
- Modify: `surogates/harness/loop.py` — delete `_should_abort_before_llm_response` (lines 599-636) and inline the interrupt check at its sole call site (lines 1854-1858)

**Interfaces:**
- Consumes: `self._check_interrupt()` (existing).
- Produces: no new symbols; removes `_should_abort_before_llm_response`.

- [ ] **Step 1: Verify the method has exactly one call site**

Run: `grep -rn "_should_abort_before_llm_response" surogates/ tests/`
Expected: two matches in `surogates/harness/loop.py` (the `async def` at ~599 and the call at ~1854), and **no** matches under `tests/`. If a test references it, update that test to call `_check_interrupt` instead before proceeding.

- [ ] **Step 2: Replace the call site**

In `surogates/harness/loop.py`, replace the call (lines 1848-1858, the `# 4a. Interrupt / staleness guard.` block):

```python
            # 4a. Interrupt / staleness guard.  The stream and any judge
            # LLM call above can take several seconds.  If a new
            # user.message has been appended (or the interrupt flag was
            # set) while we were busy, the buffered response belongs to a
            # turn the user has already abandoned — drop it instead of
            # attributing it to the next user message.
            if await self._should_abort_before_llm_response(
                session, llm_request_event_id,
            ):
                await self._abort_iteration_with_pause(session, saga)
                return
```

with:

```python
            # 4a. Interrupt guard.  Abort only on an explicit interrupt
            # (Stop / pause / lease loss).  A user.message that arrived
            # mid-stream is no longer dropped — it is folded in as a new
            # user turn at the next iteration boundary by the steer
            # injector, so the buffered response is delivered, not discarded.
            if self._check_interrupt():
                await self._abort_iteration_with_pause(session, saga)
                return
```

- [ ] **Step 3: Delete the now-unused method**

In `surogates/harness/loop.py`, delete the entire `_should_abort_before_llm_response` method (lines 599-636, from `async def _should_abort_before_llm_response(` through its closing `return False`).

- [ ] **Step 4: Verify it is gone and nothing references it**

Run: `grep -rn "_should_abort_before_llm_response" surogates/ tests/`
Expected: no matches.

- [ ] **Step 5: Run the loop tests to verify no regression**

Run: `pytest tests/test_loop_turn_id.py tests/test_wake_stranded_user_message.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py
git commit -m "feat(harness): deliver buffered response instead of dropping on new message"
```

---

## Task 5: Boundary injector, steer cursor, and turn-metadata reset in `_run_loop`

At each iteration boundary, fold in queued steer messages as one new user turn and keep the same wake going. Reset `turn_id` and the turn-local iteration index when a steer message is incorporated so per-turn correlation in the UI stays correct.

**Files:**
- Modify: `surogates/harness/llm_call.py` — `_stamp_turn_meta` and `call_llm_with_retry` metadata plumbing
- Modify: `surogates/harness/loop.py` — `_run_loop` (init block ~1306-1320; loop top ~1418-1489; every turn-metadata payload/call site that currently uses `iteration - 1`), `_request_final_summary`
- Test: `tests/test_steer_turn_meta.py` (create)
- Test: `tests/test_steer_loop.py` (create)

**Interfaces:**
- Consumes: `_collect_steer_messages` (Task 3); `all_events` param (existing); `EventType.USER_MESSAGE`; `uuid4` (already imported).
- Produces: two new `_run_loop` locals — `steer_cursor: int` and `turn_base_iteration: int` — plus `turn_iteration_index` used in event payloads.
- Produces: `call_llm_with_retry(..., iteration_index: int | None = None, ...)` and `_request_final_summary(..., iteration_index: int | None = None, ...)`. Existing callers keep the old behavior when the new argument is omitted.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_steer_loop.py`. This reuses the scaffolding shape from `tests/test_loop_turn_id.py` (`AgentHarness.__new__` + manual attributes). Define a local helper so the file is self-contained:

```python
"""Mid-turn steering: queued user messages fold into the running wake."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Session


def _make_loop_harness(*, session_store: Any, budget: IterationBudget | None = None) -> AgentHarness:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = session_store
    harness._llm = AsyncMock()
    harness._tools = MagicMock()
    harness._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
    harness._worker_id = "test-worker"
    harness._budget = budget or IterationBudget(max_total=6)
    harness._compressor = SimpleNamespace(context_length=1000, _context_window=200_000)
    harness._prompt = SimpleNamespace(has_agents=False)
    harness._redis = None
    harness._sandbox_pool = None
    harness._browser_pool = None
    harness._browser_control = None
    harness._storage = None
    harness._api_client = None
    harness._session_factory = None
    harness._vision_client = None
    harness._vision_model = ""
    harness._advisor_client = None
    harness._advisor_model = ""
    harness._advisor_max_calls_per_turn = 0
    harness._advisor_max_tokens = 0
    harness._checkpoints_enabled = False
    harness._saga_enabled = False
    harness._saga_settings = None
    harness._log_policy_allowed = False
    harness._memory_manager = None
    harness._memory_nudge_interval = 0
    harness._turns_since_memory = 0
    harness._skill_nudge_interval = 0
    harness._iters_since_skill = 0
    harness._user_turn_count = 0
    harness._thinking_disabled_for_turn = False
    harness._streaming_enabled = False
    harness._default_model = "test-model"
    harness._current_model = "test-model"
    harness._background_tasks = set()
    harness._turn_summarizer = None
    harness._pending_iteration_summary_tasks = {}
    harness._completed_iteration_summaries = {}
    harness._turn_started_at = None
    harness._interrupt_requested = False
    harness._interrupt_message = None
    harness._system_prompt_cache = MagicMock()
    harness._system_prompt_cache.is_valid = MagicMock(return_value=False)
    harness._system_prompt_cache.invalidate = MagicMock(return_value=None)
    harness._cost_tracker = None
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_consult_required_advisor = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._maybe_continue_outcome = AsyncMock(return_value=False)
    harness._maybe_run_mission_evaluator_for_session = AsyncMock(return_value=None)
    harness._mission_has_pending_work = AsyncMock(return_value=False)
    harness._maybe_summarize_iteration = AsyncMock(return_value=None)
    harness._complete_session = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    harness._provider_rate_limit_guard = MagicMock(return_value=None)
    harness._compress_context_callback = MagicMock(return_value=lambda *a, **k: None)
    # Collaborators on the tool-execution path (used only by the mid-tool
    # steer test). Mocked so a tool-calling iteration can run end-to-end.
    harness._inject_checkpoint_hashes = AsyncMock(return_value=None)
    harness._dynamic_loop_wait_succeeded = MagicMock(return_value=False)
    harness._active_executor = None
    harness._credential_vault = None
    harness._summary_client = None
    harness._summary_model = ""
    harness._media_gen = None
    harness._turn_gate = None
    harness._bundle = None
    return harness


def _make_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1",
        channel="web", status="active", config={},
        created_at=now, updated_at=now,
    )


def _steer_event(eid: int, text: str):
    return SimpleNamespace(id=eid, data={"content": text})


async def _drive(harness, responses, monkeypatch, *, all_events=None):
    call_log = iter(responses)

    async def fake_call_llm_with_retry(**_kwargs):
        try:
            return next(call_log)
        except StopIteration as exc:
            raise AssertionError("loop drove more iterations than scripted") from exc

    monkeypatch.setattr(
        "surogates.harness.loop.call_llm_with_retry", fake_call_llm_with_retry,
    )
    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())
    await harness._run_loop(
        session, [{"role": "user", "content": "do the task"}],
        "system", lease, all_events=all_events or [],
    )
    return [
        (c.args[1], c.args[2]) for c in harness._store.emit_event.await_args_list
    ]


def _tool_call_response(tc_id: str):
    return (
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": tc_id, "type": "function",
                         "function": {"name": "noop", "arguments": "{}"}}]},
        {"model": "test-model", "finish_reason": "tool_calls",
         "input_tokens": 1, "output_tokens": 1},
    )


def _final_response(text: str):
    return (
        {"role": "assistant", "content": text, "tool_calls": None},
        {"model": "test-model", "finish_reason": "stop",
         "input_tokens": 1, "output_tokens": 1},
    )


def _patch_tool_exec(monkeypatch, tool_results):
    """Patch the module-level execute_tool_calls the non-streaming path uses."""
    async def fake_execute_tool_calls(tool_calls_raw, **_kwargs):
        return list(tool_results)
    monkeypatch.setattr(
        "surogates.harness.loop.execute_tool_calls", fake_execute_tool_calls,
    )


@pytest.mark.asyncio
async def test_steer_message_starts_new_turn_id_mid_wake(monkeypatch):
    # get_events sequence: iter1 loop-top (none) -> iter2 loop-top (a steer
    # message arrived during iter1's tool call) -> iter2 completion drain (none).
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(
        side_effect=[[], [_steer_event(50, "also do Z")], []],
    )
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    # iter1 emits a tool call; the patched executor returns its result; the
    # iter2 loop-top boundary injects the steer message; iter2 is the final.
    _patch_tool_exec(monkeypatch, [{"role": "tool", "tool_call_id": "X", "content": "ok"}])

    emits = await _drive(
        harness,
        responses=[_tool_call_response("X"), _final_response("done")],
        monkeypatch=monkeypatch,
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    # second iteration belongs to a NEW turn with iteration_index reset to 0
    assert req_payloads[0]["turn_id"] != req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 0


@pytest.mark.asyncio
async def test_no_steer_message_keeps_single_turn(monkeypatch):
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)
    _patch_tool_exec(monkeypatch, [{"role": "tool", "tool_call_id": "X", "content": "ok"}])

    emits = await _drive(
        harness,
        responses=[_tool_call_response("X"), _final_response("done")],
        monkeypatch=monkeypatch,
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    # same turn across both iterations; index increments
    assert req_payloads[0]["turn_id"] == req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 1


@pytest.mark.asyncio
async def test_initial_user_message_not_re_incorporated(monkeypatch):
    # all_events already contains the initial user.message at id 50; the
    # boundary query must not re-inject it.
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])  # nothing past the cursor
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    emits = await _drive(
        harness,
        responses=[_final_response("done")],
        monkeypatch=monkeypatch,
        all_events=[SimpleNamespace(id=50, type=EventType.USER_MESSAGE.value, data={"content": "do the task"})],
    )
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 1
    # get_events was queried with after >= 50 (cursor seeded from all_events)
    first_after = store.get_events.await_args_list[0].kwargs["after"]
    assert first_after >= 50
```

Also create `tests/test_steer_turn_meta.py`:

```python
"""LLM streaming metadata can use a turn-local iteration index."""
from __future__ import annotations

from surogates.harness.llm_call import _stamp_turn_meta


def test_stamp_turn_meta_accepts_turn_local_iteration_index():
    payload = {"content": "delta"}
    out = _stamp_turn_meta(
        payload,
        iteration=9,
        turn_id="turn-2",
        iteration_index=0,
    )
    assert out is payload
    assert out == {
        "content": "delta",
        "turn_id": "turn-2",
        "iteration_index": 0,
    }


def test_stamp_turn_meta_falls_back_to_iteration_when_no_override():
    payload = {}
    _stamp_turn_meta(payload, iteration=3, turn_id="turn-1")
    assert payload == {"turn_id": "turn-1", "iteration_index": 2}


def test_stamp_turn_meta_noops_without_turn_id_even_with_override():
    payload = {}
    _stamp_turn_meta(payload, iteration=3, turn_id=None, iteration_index=0)
    assert payload == {}
```

Note: the non-streaming path calls the **module-level** `execute_tool_calls`
(imported in `loop.py` from `surogates.harness.tool_exec`, called around line
2292), so the test patches `surogates.harness.loop.execute_tool_calls` — the
same patching style as `call_llm_with_retry`. The tool-calling iteration touches
several harness collaborators (`_inject_checkpoint_hashes`,
`_dynamic_loop_wait_succeeded`, `_active_executor`, `_credential_vault`,
`_summary_client`, `_media_gen`, `_turn_gate`, `_bundle`), all pre-set in the
scaffold above. If running the test surfaces an `AttributeError` for another
collaborator on this path, set it to a `MagicMock()` / `AsyncMock()` in
`_make_loop_harness` — that is expected scaffolding work, not a design problem.
The authoritative turn-metadata assertions also run through the tool-free
completion-drain path in Task 6, which has no such dependencies.

- [ ] **Step 2: Confirm the tool-execution seam (already identified)**

The non-streaming loop path calls the module-level `execute_tool_calls`
(`surogates/harness/loop.py:2292`), patched in the test via
`surogates.harness.loop.execute_tool_calls`. Confirm it is still the seam:

Run: `grep -n "execute_tool_calls(" surogates/harness/loop.py`
Expected: the call site around line 2292 in `_run_loop`. The behavioral
assertions (turn_id / iteration_index) do not depend on tool internals.

- [ ] **Step 3: Run tests to verify metadata override is missing**

Run: `pytest tests/test_steer_turn_meta.py -v`
Expected: FAIL with `TypeError: _stamp_turn_meta() got an unexpected keyword argument 'iteration_index'`.

- [ ] **Step 4: Add turn-local metadata support to `llm_call.py`**

In `surogates/harness/llm_call.py`, change `_stamp_turn_meta` from:

```python
def _stamp_turn_meta(
    payload: dict[str, Any],
    *,
    iteration: int,
    turn_id: str | None,
) -> dict[str, Any]:
```

to:

```python
def _stamp_turn_meta(
    payload: dict[str, Any],
    *,
    iteration: int,
    turn_id: str | None,
    iteration_index: int | None = None,
) -> dict[str, Any]:
```

Then replace its body with:

```python
    if turn_id is not None:
        payload["turn_id"] = turn_id
        if iteration_index is None:
            payload["iteration_index"] = max(int(iteration) - 1, 0)
        else:
            payload["iteration_index"] = max(int(iteration_index), 0)
    return payload
```

In the `call_llm_with_retry(...)` signature, add `iteration_index: int | None = None`
immediately after `turn_id: str | None = None`.

Every `_stamp_turn_meta(...)` call in `llm_call.py` must pass the override:

```python
_stamp_turn_meta(
    {...},
    iteration=iteration,
    turn_id=turn_id,
    iteration_index=iteration_index,
)
```

Use this search to verify all call sites were updated:

Run: `grep -n "_stamp_turn_meta(" surogates/harness/llm_call.py`
Expected: one function definition plus every call block containing `iteration_index=iteration_index`.

- [ ] **Step 5: Run metadata tests to verify they pass**

Run: `pytest tests/test_steer_turn_meta.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Seed the steer cursor and turn-base at loop init**

In `surogates/harness/loop.py`, in `_run_loop`, just after `turn_id = str(uuid4())` (line 1306) and the `self._turn_started_at` assignment, add:

```python
        # Steer cursor: highest user-message event already folded into the
        # replayed ``messages``.  Mid-wake real follow-ups past this cursor
        # are incorporated at iteration boundaries (see the loop top).  Kept
        # separate from the durable session cursor, which tracks crash
        # recovery, not in-memory message incorporation.
        steer_cursor = max(
            (
                event.id
                for event in (all_events or [])
                if event.type == EventType.USER_MESSAGE.value
            ),
            default=0,
        )
        # Iteration index is reported per user turn, not per wake.  When a
        # steer message starts a new turn mid-wake, this base advances so the
        # new turn's first model call reports iteration_index 0.
        turn_base_iteration = 0
```

- [ ] **Step 7: Inject queued messages at the loop top + reset turn metadata**

In `_run_loop`, at the loop top, **after** the interrupt check block (lines 1430-1433) and **before** the checkpoint reset (line 1435), insert:

```python
            # --- Mid-turn steering ---
            # Fold in any real user messages that arrived since the last
            # boundary as one coalesced new user turn, then keep going in
            # the same wake.  The interrupt check above already ran, so an
            # explicit Stop always wins over a steer.
            steer_message, steer_cursor = await self._collect_steer_messages(
                session.id, steer_cursor,
            )
            if steer_message is not None:
                messages.append(steer_message)
                turn_id = str(uuid4())
                turn_base_iteration = iteration - 1
                self._pending_iteration_summary_tasks = {}
                self._completed_iteration_summaries = {}
                logger.info(
                    "Session %s: incorporated steer message at iteration %d "
                    "(new turn_id=%s)",
                    session.id, iteration, turn_id,
                )
```

- [ ] **Step 8: Report iteration index per turn**

In `_run_loop`, replace every user-turn metadata payload expression that reports
`iteration - 1` with the turn-local index. First compute it once per iteration —
add immediately after the steer block from Step 7:

```python
            turn_iteration_index = iteration - 1 - turn_base_iteration
```

Then update these sites:

1. Main-loop `call_llm_with_retry(...)` call: add `iteration_index=turn_iteration_index,` next to `turn_id=turn_id,`.
2. LLM_REQUEST payload (line ~1487): change `"iteration_index": iteration - 1,` to `"iteration_index": turn_iteration_index,`.
3. Manual LLM_THINKING payload (line ~1711): change `"iteration_index": iteration - 1,` to `"iteration_index": turn_iteration_index,`.
4. LLM_RESPONSE payload (line ~1861): change `response_data["iteration_index"] = iteration - 1` to `response_data["iteration_index"] = turn_iteration_index`.
5. No-tool `_maybe_summarize_iteration(...)` call (line ~2142): change `iteration_index=iteration - 1,` to `iteration_index=turn_iteration_index,`.
6. Tool-result `_maybe_summarize_iteration(...)` call (line ~2445): change `iteration_index=iteration - 1,` to `iteration_index=turn_iteration_index,`.
7. Budget-exhaustion `_request_final_summary(...)` call inside the `if not self._budget.consume()` branch: add `iteration_index=turn_iteration_index,`.
8. Budget-exhaustion `_request_final_summary(...)` call after the `while` loop: add `iteration_index=max(iteration - 1 - turn_base_iteration, 0),`.

(The wake-wide `iteration` counter still drives the budget and loop control; only the reported index is turn-local.)

- [ ] **Step 9: Thread the turn-local index through `_request_final_summary`**

In `surogates/harness/loop.py`, update `_request_final_summary(...)` so the
signature includes the optional index:

```python
        turn_id: str | None = None,
        iteration_index: int | None = None,
```

In the `_request_final_summary` internal `call_llm_with_retry(...)` call, add:

```python
                iteration_index=iteration_index,
```

Replace:

```python
                final_payload["iteration_index"] = max(self._budget.used - 1, 0)
```

with:

```python
                final_payload["iteration_index"] = (
                    max(int(iteration_index), 0)
                    if iteration_index is not None
                    else max(self._budget.used - 1, 0)
                )
```

- [ ] **Step 10: Run the new and existing loop tests**

Run: `pytest tests/test_steer_turn_meta.py tests/test_steer_loop.py tests/test_loop_turn_id.py -v`
Expected: PASS. `test_loop_turn_id.py` still passes because with no steer messages `turn_base_iteration` stays 0, so `turn_iteration_index == iteration - 1` exactly as before.

- [ ] **Step 11: Commit**

```bash
git add surogates/harness/llm_call.py surogates/harness/loop.py tests/test_steer_turn_meta.py tests/test_steer_loop.py
git commit -m "feat(harness): fold queued user messages into the running wake at boundaries"
```

---

## Task 6: Drain a follow-up at completion instead of completing + re-waking

When the final (no-tool-calls) response is ready but a follow-up has already arrived, deliver the response, then continue the same wake as a new turn rather than completing and re-waking.

**Files:**
- Modify: `surogates/harness/loop.py` — the no-tool-calls completion branch, immediately before `_complete_session` (around line 2148-2151)
- Test: `tests/test_steer_loop.py` (extend)

**Interfaces:**
- Consumes: `_collect_steer_messages` (Task 3), `steer_cursor` / `turn_id` / `turn_base_iteration` locals (Task 5).
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_steer_loop.py`:

```python
@pytest.mark.asyncio
async def test_followup_at_completion_continues_instead_of_completing(monkeypatch):
    # iter1 returns a final response (no tool calls); a follow-up is waiting,
    # so the wake must NOT complete — it continues as a new turn and iter2
    # produces the real final response.
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    # boundary query at iter1 top: nothing; completion drain at iter1: a
    # follow-up; boundary query at iter2 top: nothing; drain at iter2: none.
    store.get_events = AsyncMock(side_effect=[
        [],                              # iter1 loop-top steer check
        [_steer_event(60, "one more thing")],  # iter1 completion drain
        [],                              # iter2 loop-top steer check
        [],                              # iter2 completion drain
    ])
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    emits = await _drive(
        harness,
        responses=[_final_response("first answer"), _final_response("second answer")],
        monkeypatch=monkeypatch,
    )
    # completion happened exactly once, after the second response
    assert harness._complete_session.await_count == 1
    req_payloads = [p for t, p in emits if t == EventType.LLM_REQUEST]
    assert len(req_payloads) == 2
    assert req_payloads[0]["turn_id"] != req_payloads[1]["turn_id"]
    assert req_payloads[1]["iteration_index"] == 0


@pytest.mark.asyncio
async def test_no_followup_completes_normally(monkeypatch):
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])  # never any steer messages
    store.execute = AsyncMock(return_value=None)
    harness = _make_loop_harness(session_store=store)

    await _drive(harness, responses=[_final_response("done")], monkeypatch=monkeypatch)
    assert harness._complete_session.await_count == 1
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `pytest tests/test_steer_loop.py::test_followup_at_completion_continues_instead_of_completing -v`
Expected: FAIL — `_complete_session` is awaited once but only one `LLM_REQUEST` is emitted (the wake completes on iter1 instead of continuing).

- [ ] **Step 3: Add the completion drain**

In `surogates/harness/loop.py`, in the no-tool-calls branch, **after** `_maybe_summarize_iteration` (line ~2139-2146) and **before** the `await self._complete_session(...)` call (line ~2151), insert:

```python
                # Before completing, fold in any follow-up that arrived while
                # this final response was being produced.  Deliver the
                # response (already emitted + appended above), then keep the
                # wake going as a new user turn instead of completing and
                # re-waking.
                followup, steer_cursor = await self._collect_steer_messages(
                    session.id, steer_cursor,
                )
                if followup is not None:
                    messages.append(followup)
                    turn_id = str(uuid4())
                    turn_base_iteration = iteration
                    self._pending_iteration_summary_tasks = {}
                    self._completed_iteration_summaries = {}
                    logger.info(
                        "Session %s: follow-up arrived at completion; "
                        "continuing as a new turn (turn_id=%s)",
                        session.id, turn_id,
                    )
                    continue
```

(`turn_base_iteration = iteration` — not `iteration - 1` — because `continue` runs the loop top, which does `iteration += 1` first; the next pass then reports `iteration_index = (iteration+1) - 1 - iteration = 0`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_steer_loop.py -v`
Expected: PASS (all steer-loop tests)

- [ ] **Step 5: Run the broader harness suite for regressions**

Run: `pytest tests/test_steer_turn_meta.py tests/test_loop_turn_id.py tests/test_wake_stranded_user_message.py tests/test_board_replay_hydration.py tests/harness/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py tests/test_steer_loop.py
git commit -m "feat(harness): continue the wake when a follow-up lands at completion"
```

---

## Task 7: Full-suite regression sweep

A final guard that the change did not disturb adjacent harness behavior.

**Files:** none (verification only).

- [ ] **Step 1: Run the harness + replay + loop test groups**

Run:
```bash
pytest tests/test_steer_coalesce.py tests/test_steer_replay_resequence.py \
       tests/test_steer_collect.py tests/test_steer_turn_meta.py \
       tests/test_steer_loop.py \
       tests/test_loop_turn_id.py tests/test_wake_stranded_user_message.py \
       tests/test_board_replay_hydration.py tests/test_loop_tool_recovery.py \
       tests/harness/ tests/integration/test_attachment_history_replay.py -v
```
Expected: all PASS.

- [ ] **Step 2: Run the broader unit suite (skip the opt-in browser/live markers, already excluded by addopts)**

Run: `pytest tests/ -q`
Expected: PASS (or only pre-existing failures unrelated to this change — if any failure mentions `steer`, `_rebuild_messages`, `iteration_index`, `turn_id`, or `_should_abort`, fix it before finishing).

- [ ] **Step 3: Final review of the diff**

Run: `git log --oneline master..HEAD` and `git diff master --stat`
Confirm the diff touches only `surogates/harness/loop.py`, `surogates/harness/loop_context_replay.py`, `surogates/harness/llm_call.py`, and the five new test files — no changes to `send_message`, the orchestrator, the Redis interrupt channel, or any DB schema, per the design's non-goals.

---

## Self-review notes (spec coverage)

- **Injection point = next boundary** → Task 5 (loop-top injector).
- **Coalescing** → Task 1 (`coalesce_user_messages`), used by Tasks 3, 5, 6 and the replay re-sequencer (Task 2).
- **Stop preserved** → Task 4 keeps the explicit-interrupt abort; the injector runs after the interrupt check (Task 5).
- **Steer cursor, separate from durable cursor** → Task 5 (local `steer_cursor`).
- **Non-synthetic filter** → Tasks 2 and 3.
- **Replay re-sequencing via `tool_call_id` set** → Task 2.
- **Turn-metadata reset (`turn_id`, turn-local `iteration_index`)** → Tasks 5 and 6, including `llm.delta`, `llm.thinking`, `llm.request`, `llm.response`, iteration-summary, and final-summary payloads.
- **Attachment/image steer** → covered by reusing `build_user_message_dict` (Tasks 3) + multimodal coalescing (Task 1), exercised in `test_steer_coalesce.py` and `test_steer_collect.py`.
- **Completion drain** → Task 6.
- **Non-goals (no API/orchestrator/schema change)** → verified in Task 7 Step 3.
