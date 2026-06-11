"""read_board and expand_note handlers."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from surogates.board.store import BoardStore
from surogates.board.tools import _expand_note_handler, _read_board_handler
from surogates.session.events import EventType


async def _passthrough(drafts):
    return list(drafts), []


async def _seed(session_factory, org_id, group_id, writer_id, contents):
    board = BoardStore(session_factory)
    out = []
    for type_, content, ref in contents:
        r = await board.admit(
            raw_notes=[{"type": type_, "content": content, "ref": ref}],
            org_id=org_id, group_id=group_id,
            writer_session_id=writer_id, writer_label="coord",
            verifier=_passthrough,
            max_claims_per_writer=2, max_notes_per_group=300,
            claim_ttl_seconds=300,
        )
        out.extend(r.admitted)
    return out


def _kwargs(session, session_factory, session_store, group_id):
    return dict(
        session_id=str(session.id),
        session_factory=session_factory,
        session_store=session_store,
        tenant=SimpleNamespace(org_id=session.org_id),
        session_config={"context_group_id": str(group_id)},
        storage=None,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_read_board_renders_current_state_and_advances_cursor(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    await _seed(session_factory, org_id, group_id, parent_session.id, [
        ("FACT", "store.py:10 caches settings", None),
        ("FAIL", "approach z dead-ends in q.py", None),
    ])
    out = await _read_board_handler(
        {}, **_kwargs(parent_session, session_factory, session_store, group_id),
    )
    assert "store.py:10" in out and "approach z" in out

    # Cursor advanced to current max seq.
    board = BoardStore(session_factory)
    refreshed = await session_store.get_session(parent_session.id)
    assert refreshed.config.get("board_cursor") == await board.max_seq(group_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_read_board_type_filter(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    await _seed(session_factory, org_id, group_id, parent_session.id, [
        ("FACT", "store.py:10 caches settings", None),
        ("FAIL", "approach z dead-ends in q.py", None),
    ])
    out = await _read_board_handler(
        {"types": ["FAIL"]},
        **_kwargs(parent_session, session_factory, session_store, group_id),
    )
    assert "approach z" in out and "store.py:10" not in out


@pytest.mark.asyncio(loop_scope="session")
async def test_read_board_empty_board(
    parent_session, session_factory, session_store,
):
    out = await _read_board_handler(
        {},
        **_kwargs(parent_session, session_factory, session_store,
                  parent_session.id),
    )
    assert out == "(board is empty)"


@pytest.mark.asyncio(loop_scope="session")
async def test_expand_note_event_ref_within_group(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    event_id = await session_store.emit_event(
        parent_session.id, EventType.TOOL_RESULT,
        {"tool_call_id": "tc1",
         "content": "the long underlying detail " * 20},
    )
    notes = await _seed(session_factory, org_id, group_id, parent_session.id, [
        ("FACT", "summarized finding f.py:1",
         {"kind": "event", "session_id": str(parent_session.id),
          "event_id": event_id}),
    ])
    out = json.loads(await _expand_note_handler(
        {"note_id": notes[0].id},
        **_kwargs(parent_session, session_factory, session_store, group_id),
    ))
    assert "the long underlying detail" in out["detail"]


@pytest.mark.asyncio(loop_scope="session")
async def test_expand_note_rejects_target_outside_group(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    # Ref pointing at a session that is NOT a member of this group.
    outsider_sid = uuid.uuid4()
    notes = await _seed(session_factory, org_id, group_id, parent_session.id, [
        ("FACT", "sneaky ref g.py:2",
         {"kind": "event", "session_id": str(outsider_sid), "event_id": 1}),
    ])
    out = json.loads(await _expand_note_handler(
        {"note_id": notes[0].id},
        **_kwargs(parent_session, session_factory, session_store, group_id),
    ))
    assert "error" in out


@pytest.mark.asyncio(loop_scope="session")
async def test_expand_note_without_ref_errors(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    notes = await _seed(session_factory, org_id, group_id, parent_session.id, [
        ("FACT", "no ref here h.py:3", None),
    ])
    out = json.loads(await _expand_note_handler(
        {"note_id": notes[0].id},
        **_kwargs(parent_session, session_factory, session_store, group_id),
    ))
    assert out["error"] == "note has no expandable detail"


@pytest.mark.asyncio(loop_scope="session")
async def test_expand_note_unknown_note_errors(
    parent_session, session_factory, session_store,
):
    out = json.loads(await _expand_note_handler(
        {"note_id": 99_999_999},
        **_kwargs(parent_session, session_factory, session_store,
                  parent_session.id),
    ))
    assert "not found" in out["error"]
