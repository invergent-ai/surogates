"""BoardNote ORM model: insert, defaults, seq monotonicity."""
import uuid

import pytest
from sqlalchemy import select, update

from surogates.db.models import BoardNote, board_note_seq


@pytest.mark.asyncio(loop_scope="session")
async def test_board_note_insert_defaults(session_factory, org_id, parent_session):
    group_id = uuid.uuid4()
    async with session_factory() as db:
        note = BoardNote(
            org_id=org_id,
            group_id=group_id,
            writer_session_id=parent_session.id,
            writer_label="coord",
            type="FACT",
            content="channels/slack.py:214 bypasses outbox for DMs",
        )
        db.add(note)
        await db.commit()
        await db.refresh(note)

    assert note.id > 0
    assert note.seq > 0
    assert note.status == "active"
    assert note.ref is None
    assert note.expires_at is None
    assert note.created_at is not None
    assert note.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_board_note_seq_monotonic_across_insert_and_update(
    session_factory, org_id, parent_session,
):
    group_id = uuid.uuid4()
    async with session_factory() as db:
        a = BoardNote(
            org_id=org_id, group_id=group_id,
            writer_session_id=parent_session.id, writer_label="w1aa",
            type="RESULT",
            content="outcome=x|evidence=pytest passed|risk=none",
        )
        b = BoardNote(
            org_id=org_id, group_id=group_id,
            writer_session_id=parent_session.id, writer_label="w1aa",
            type="FAIL", content="socket-mode tests blocked in sandbox",
        )
        db.add_all([a, b])
        await db.commit()
        await db.refresh(a)
        await db.refresh(b)
        seq_a, seq_b = a.seq, b.seq

        # Status transition re-bumps seq past every prior value.
        await db.execute(
            update(BoardNote)
            .where(BoardNote.id == a.id)
            .values(status="superseded", seq=board_note_seq.next_value())
        )
        await db.commit()

    async with session_factory() as db:
        bumped = (await db.execute(
            select(BoardNote).where(BoardNote.id == a.id)
        )).scalar_one()

    assert seq_b > seq_a
    assert bumped.seq > seq_b
    assert bumped.status == "superseded"
