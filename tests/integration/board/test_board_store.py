"""BoardStore DB behavior: admission, supersede, renewal, queries, purge."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update as sa_update

from surogates.board.store import BoardStore
from surogates.db.models import BoardNote, Session as ORMSession


async def _passthrough_verifier(drafts):
    return list(drafts), []


@pytest.fixture
def board_store(session_factory):
    return BoardStore(session_factory)


def _admit_kwargs(org_id, group_id, writer_session_id, **overrides):
    kwargs = dict(
        org_id=org_id, group_id=group_id,
        writer_session_id=writer_session_id, writer_label="coord",
        verifier=_passthrough_verifier,
        max_claims_per_writer=2, max_notes_per_group=300,
        claim_ttl_seconds=300,
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio(loop_scope="session")
async def test_admit_inserts_and_returns_admitted(
    board_store, org_id, parent_session,
):
    group_id = uuid.uuid4()
    result = await board_store.admit(
        raw_notes=[
            {"type": "FACT", "content": "store.py:10 caches settings"},
            {"type": "BOGUS", "content": "rejected by precheck"},
        ],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    assert len(result.admitted) == 1
    assert result.admitted[0].type == "FACT"
    assert len(result.rejected) == 1

    active = await board_store.active_notes(group_id)
    assert [n.content for n in active] == ["store.py:10 caches settings"]


@pytest.mark.asyncio(loop_scope="session")
async def test_admit_supersedes_prior_result_of_same_writer(
    board_store, org_id, parent_session,
):
    group_id = uuid.uuid4()
    r1 = await board_store.admit(
        raw_notes=[{"type": "RESULT",
                    "content": "outcome=v1|evidence=ran 1 test|risk=-"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    r2 = await board_store.admit(
        raw_notes=[{"type": "RESULT",
                    "content": "outcome=v2|evidence=ran 2 tests|risk=-"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    active = await board_store.active_notes(group_id)
    assert [n.content for n in active] == [
        "outcome=v2|evidence=ran 2 tests|risk=-"
    ]

    old = await board_store.get_note(r1.admitted[0].id)
    assert old.status == "superseded"
    # The transition bumped seq past the original insert seq.
    assert old.seq > r1.admitted[0].seq
    assert r2.admitted[0].status == "active"


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_renewal_refreshes_expiry_and_bumps_seq(
    board_store, org_id, parent_session,
):
    group_id = uuid.uuid4()
    r1 = await board_store.admit(
        raw_notes=[{"type": "CLAIM", "content": "claiming auth"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    first_id = r1.admitted[0].id
    first_seq = r1.admitted[0].seq
    first_expiry = r1.admitted[0].expires_at

    r2 = await board_store.admit(
        raw_notes=[{"type": "CLAIM", "content": "claiming auth"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    assert not r2.admitted and not r2.rejected and r2.renewed == [first_id]

    renewed = await board_store.get_note(first_id)
    assert renewed.expires_at >= first_expiry
    assert renewed.seq > first_seq
    assert renewed.status == "active"


@pytest.mark.asyncio(loop_scope="session")
async def test_changes_since_cursor(board_store, org_id, parent_session):
    group_id = uuid.uuid4()
    r1 = await board_store.admit(
        raw_notes=[{"type": "FACT", "content": "first fact a.py:1"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    cursor = r1.admitted[0].seq
    assert await board_store.changes_since(group_id, cursor) == []

    await board_store.admit(
        raw_notes=[{"type": "FAIL", "content": "approach b dead-ends"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    changed = await board_store.changes_since(group_id, cursor)
    assert [n.content for n in changed] == ["approach b dead-ends"]
    assert await board_store.max_seq(group_id) == changed[0].seq


@pytest.mark.asyncio(loop_scope="session")
async def test_expire_due_claims_flips_status_and_bumps_seq(
    board_store, org_id, parent_session,
):
    group_id = uuid.uuid4()
    r = await board_store.admit(
        raw_notes=[{"type": "CLAIM", "content": "claiming doomed work"}],
        **_admit_kwargs(org_id, group_id, parent_session.id,
                        claim_ttl_seconds=0),
    )
    note_id = r.admitted[0].id
    n_expired = await board_store.expire_due_claims()
    assert n_expired >= 1
    expired = await board_store.get_note(note_id)
    assert expired.status == "expired"


@pytest.mark.asyncio(loop_scope="session")
async def test_purge_terminal_root_groups(
    board_store, org_id, session_factory,
):
    # Root session terminal + backdated past the cutoff → notes purged.
    root_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=root_id, org_id=org_id, agent_id="orchestrator",
            channel="web", status="completed", config={},
        ))
        await db.commit()
    await board_store.admit(
        raw_notes=[{"type": "FACT", "content": "doomed group note x.py:1"}],
        **_admit_kwargs(org_id, root_id, root_id),
    )
    async with session_factory() as db:
        # sessions.updated_at is a naive-UTC TIMESTAMP column.
        await db.execute(
            sa_update(ORMSession).where(ORMSession.id == root_id).values(
                updated_at=(
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(days=30)
                ),
            )
        )
        await db.commit()

    purged = await board_store.purge_terminal_root_groups(older_than_days=7)
    assert purged >= 1
    assert await board_store.active_notes(root_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_purge_stale_rows(board_store, org_id, parent_session, session_factory):
    group_id = uuid.uuid4()
    r1 = await board_store.admit(
        raw_notes=[{"type": "RESULT", "content": "outcome=a|evidence=ran|risk=-"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    await board_store.admit(
        raw_notes=[{"type": "RESULT", "content": "outcome=b|evidence=ran|risk=-"}],
        **_admit_kwargs(org_id, group_id, parent_session.id),
    )
    # Backdate the superseded row past the cutoff.
    async with session_factory() as db:
        await db.execute(
            sa_update(BoardNote).where(BoardNote.id == r1.admitted[0].id).values(
                updated_at=datetime.now(timezone.utc) - timedelta(days=30)
            )
        )
        await db.commit()

    purged = await board_store.purge_stale_rows(older_than_days=7)
    assert purged >= 1
    assert await board_store.get_note(r1.admitted[0].id) is None
    # The active replacement survives.
    active = await board_store.active_notes(group_id)
    assert [n.content for n in active] == ["outcome=b|evidence=ran|risk=-"]


@pytest.mark.asyncio(loop_scope="session")
async def test_purge_orphaned_groups(board_store, org_id, parent_session):
    orphan_group = uuid.uuid4()  # no session row with this id exists
    await board_store.admit(
        raw_notes=[{"type": "FACT", "content": "orphaned note x.py:1"}],
        **_admit_kwargs(org_id, orphan_group, parent_session.id),
    )
    purged = await board_store.purge_orphaned_groups()
    assert purged >= 1
    assert await board_store.active_notes(orphan_group) == []
