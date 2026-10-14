"""Escalations recorded before they had a table of their own become records.

One raised on a step's invitation keeps the invitation's id; one raised in
free conversation, known only from the team's alerts, takes the id its
alerts' key carries.  Applying the retrofit again adds nothing.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from surogates.db.engine import apply_observability_ddl
from surogates.db.models import Base

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def engine(pg_url):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    name = f"escalations_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(pg_url.rsplit("/", 1)[0] + f"/{name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _alert(conn, *, org, person, key, severity="yellow", reason="x"):
    await conn.execute(text(
        "INSERT INTO outreach_messages (id, org_id, agent_id, user_id, platform,"
        " channel_identifier, kind, text, created_by, subject_user_id, dedupe_key,"
        " severity, alert_reason)"
        " VALUES (:id, :org, 'a1', :member, 'whatsapp', 'pn', 'alert', :reason, 'agent',"
        " :person, :key, :severity, :reason)"
    ), {"id": uuid.uuid4(), "org": org, "member": uuid.uuid4(), "person": person,
        "key": key, "severity": severity, "reason": reason})


async def test_earlier_escalations_become_records_once(engine):
    org, person, step_inv, free = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO program_invitations (id, occurrence_id, program_id, org_id, agent_id,"
            " user_id, platform, platform_user_id, escalation_state, reason)"
            " VALUES (:id, :occ, :prog, :org, 'a1', :person, 'whatsapp', '40700000001',"
            " 'raised', 'No answer to 3 openers in a row; team alerted')"
        ), {"id": step_inv, "occ": uuid.uuid4(), "prog": uuid.uuid4(), "org": org,
            "person": person})
        await _alert(conn, org=org, person=person, key=f"missed:{step_inv}:m1", severity=None,
                     reason="Ion has not answered")
        # Two team members got the same free-conversation escalation.
        for member in ("m1", "m2"):
            await _alert(conn, org=org, person=person, severity="red",
                         key=f"escalation:-:{free.hex}:{member}", reason="fever")
        await apply_observability_ddl(conn)
        await apply_observability_ddl(conn)
        rows = (await conn.execute(text(
            "SELECT id, kind, severity, reason, state, invitation_id FROM escalations"
            " ORDER BY kind"
        ))).all()
    assert [tuple(r) for r in rows] == [
        (free, "alert", "red", "fever", "raised", None),
        (step_inv, "missed_replies", None, "Ion has not answered", "raised", step_inv),
    ]
