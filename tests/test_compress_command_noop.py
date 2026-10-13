"""/compress answers with a notice about the conversation, and a pass that compacts nothing writes
no compaction.

PROD session fd74d099 had 21 messages, under the compressor's floor.  The command still wrote a
context.compact event and answered "22 → 21 messages (1 removed)": the one removed was /compress.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from surogates.harness.context import ContextCompressor
from surogates.session.events import EventType
from tests.test_steer_loop import _make_loop_harness, _make_session

HISTORY = [{"role": "user", "content": f"message {number}"} for number in range(21)]


async def _compress(harness) -> list[tuple[EventType, dict]]:
    await harness._handle_compress_command(
        _make_session(), [*HISTORY, {"role": "user", "content": "/compress"}],
        "system", SimpleNamespace(lease_token="t"),
    )
    return [(c.args[1], c.args[2]) for c in harness._store.emit_event.await_args_list]


async def test_nothing_to_compress_writes_no_compaction():
    harness = _make_loop_harness(session_store=AsyncMock())
    harness._compressor = ContextCompressor("surogate-pro", quiet_mode=True)

    [(event_type, data)] = await _compress(harness)

    assert event_type == EventType.LLM_RESPONSE
    assert data["message"]["content"] == "Nothing to compress yet."
    assert data["notice"] is True


async def test_counts_leave_out_the_command():
    harness = _make_loop_harness(session_store=AsyncMock())
    harness._compressor = SimpleNamespace(
        context_length=1000,
        compress=AsyncMock(return_value=(HISTORY[:2], {"strategy": "summary"})),
    )
    harness._memory_snapshot_cache = {}

    events = await _compress(harness)

    assert events[0][0] == EventType.CONTEXT_COMPACT
    reply = events[-1][1]
    assert reply["message"]["content"] == "Context compressed: 21 → 2 messages."
    assert reply["notice"] is True
