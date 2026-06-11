"""BoardMixin.maybe_emit_board_update: snapshot, delta, cursor, no-op."""
from __future__ import annotations

import pytest

from surogates.board.store import BoardStore
from surogates.harness.loop_board import BoardMixin
from surogates.session.events import EventType


class _Host(BoardMixin):
    """Minimal harness host exposing what the mixin needs."""

    def __init__(self, store, session_factory):
        self._store = store
        self._session_factory = session_factory


async def _passthrough(drafts):
    return list(drafts), []


async def _seed(session_factory, org_id, group_id, writer, contents):
    board = BoardStore(session_factory)
    for type_, content in contents:
        await board.admit(
            raw_notes=[{"type": type_, "content": content}],
            org_id=org_id, group_id=group_id,
            writer_session_id=writer, writer_label="coord",
            verifier=_passthrough,
            max_claims_per_writer=2, max_notes_per_group=300,
            claim_ttl_seconds=300,
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_join_snapshot_then_delta_then_noop(
    session_store, session_factory, org_id, parent_session,
):
    group_id = parent_session.id
    parent_session.config["context_group_id"] = str(group_id)
    host = _Host(session_store, session_factory)
    messages: list[dict] = []

    # Empty board: no event, cursor stays None (unjoined).
    cursor = await host.maybe_emit_board_update(parent_session, messages, None)
    assert cursor is None and messages == []

    await _seed(session_factory, org_id, group_id, parent_session.id,
                [("FACT", "join snapshot fact a.py:1")])

    # First non-empty sight: full snapshot.
    cursor = await host.maybe_emit_board_update(parent_session, messages, None)
    assert cursor is not None
    assert "Shared board" in messages[-1]["content"]
    assert "join snapshot fact" in messages[-1]["content"]

    # Nothing new: no-op.
    n_msgs = len(messages)
    cursor2 = await host.maybe_emit_board_update(parent_session, messages, cursor)
    assert cursor2 == cursor and len(messages) == n_msgs

    # New note: compact delta, cursor advances, event persisted.
    await _seed(session_factory, org_id, group_id, parent_session.id,
                [("FAIL", "delta dead end b.py:2")])
    cursor3 = await host.maybe_emit_board_update(parent_session, messages, cursor)
    assert cursor3 > cursor
    assert "[Board update]" in messages[-1]["content"]
    assert "delta dead end" in messages[-1]["content"]

    events = await session_store.get_events(
        parent_session.id, types=[EventType.BOARD_UPDATE],
    )
    assert len(events) == 2  # snapshot + delta
    assert events[0].data["kind"] == "snapshot"
    assert events[1].data["kind"] == "delta"
    # Cursor persisted for the next wake.
    refreshed = await session_store.get_session(parent_session.id)
    assert refreshed.config.get("board_cursor") == cursor3


@pytest.mark.asyncio(loop_scope="session")
async def test_no_group_key_is_total_noop(
    session_store, session_factory, parent_session,
):
    parent_session.config.pop("context_group_id", None)
    host = _Host(session_store, session_factory)
    messages: list[dict] = []
    cursor = await host.maybe_emit_board_update(parent_session, messages, None)
    assert cursor is None and messages == []


@pytest.mark.asyncio(loop_scope="session")
async def test_board_failure_never_breaks_the_loop(
    session_store, parent_session,
):
    parent_session.config["context_group_id"] = str(parent_session.id)
    # A session factory that explodes — the hook must swallow and
    # return the cursor unchanged.
    def _broken_factory():
        raise RuntimeError("db down")

    host = _Host(session_store, _broken_factory)
    messages: list[dict] = []
    cursor = await host.maybe_emit_board_update(parent_session, messages, 5)
    assert cursor == 5 and messages == []
