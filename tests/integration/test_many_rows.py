"""A statement of many rows on the real database: sent so that a stop at any of its waits ends (surogates.db.many_rows)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from surogates.db import many_rows

from surogates.db.models import Event, Session

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Each a row that fills one of the driver's packets by itself.
RESULT = "r" * 40_000


async def seed(session_factory, events: int) -> tuple[uuid.UUID, list[int]]:
    """A session of a fresh org and *events* events of it: the session and the events' ids."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(Session(id=session_id, org_id=org_id, user_id=user_id, agent_id="agent-x", config={}))
        await db.commit()
        ids = list((await db.execute(
            text(
                "INSERT INTO events (session_id, type, data) "
                "SELECT :sess, 'many.seed', '{}'::jsonb FROM generate_series(1, :n) RETURNING id"
            ),
            {"sess": session_id, "n": events},
        )).scalars())
        await db.commit()
    return session_id, ids


async def finalize(session_factory, ids: list[int]) -> None:
    """Every event's row changed in one flush, each a packet of its own: the ORM sends them in one statement."""
    async with session_factory() as db:
        for row in (await db.execute(select(Event).where(Event.id.in_(ids)))).scalars().all():
            row.type = "many.done"
            row.data = {"result": RESULT}
        await db.commit()


async def link(session_factory, session_id, parents: list[int]) -> None:
    """One event per *parents* entry added in one flush: the ORM inserts them in one statement, or in groups."""
    async with session_factory() as db:
        db.add_all([Event(session_id=session_id, type="many.link", data={"of": parent}) for parent in parents])
        await db.commit()


async def done(session_factory, ids) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(Event).where(Event.id.in_(ids), Event.type == "many.done")
        )).scalar_one()


async def links(session_factory, session_id) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(Event).where(Event.session_id == session_id, Event.type == "many.link")
        )).scalar_one()


def statements(engine, table: str):
    """Each statement on *table* the driver is given from now on, as (it has many rows, its rows)."""
    seen: list[tuple[bool, int]] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if table in statement and not statement.lstrip().upper().startswith("SELECT"):
            seen.append((executemany, len(parameters) if executemany else 1))

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine.sync_engine, "before_cursor_execute", before)


async def test_six_results_of_40_kb_changed_in_one_flush_end_when_stopped_at_any_wait(stopping, session_factory):
    _, ids = await seed(session_factory, 6)
    for at in range(200):
        await stopping.stop_at(at, finalize(stopping.session_factory, ids))
        changed = await done(session_factory, ids)
        if changed == 6:
            break
        # Stopped before its commit: none of them.
        assert changed == 0
    else:
        pytest.fail("the events were never changed")
    async with session_factory() as db:
        assert {row["result"] for row in (await db.execute(select(Event.data).where(Event.id.in_(ids)))).scalars()} == {RESULT}


@pytest.mark.slow
async def test_the_same_flush_never_ends_when_stopped_without_the_guard(stopping, session_factory, unguarded):
    """The driver's own fault, kept in sight: the day this fails, asyncpg ends such a call and the guard may go."""
    _, ids = await seed(session_factory, 6)
    with pytest.raises(AssertionError, match="the call did not end"):
        for at in range(200):
            await stopping.stop_at(at, finalize(stopping.session_factory, ids), within=3.0)
            if await done(session_factory, ids) == 6:
                break


@pytest.mark.parametrize("parents", [5, pytest.param(2_600, marks=pytest.mark.slow)])
async def test_many_rows_added_in_one_flush_are_all_or_none_when_stopped_at_any_wait(stopping, session_factory, parents):
    session_id, ids = await seed(session_factory, parents)
    for at in range(400):
        stopped = await stopping.stop_at(at, link(stopping.session_factory, session_id, ids))
        made = await links(session_factory, session_id)
        if made == parents:
            break
        assert made == 0 and stopped
    else:
        pytest.fail("the rows were never added")


async def test_few_small_rows_go_as_one_statement_and_many_in_groups_all_of_them_once(engine, session_factory):
    session_id, ids = await seed(session_factory, 2_600)
    seen, stop = statements(engine, "INSERT INTO events")
    try:
        await link(session_factory, session_id, ids[:5])
        # As it always was: one statement of the five rows.
        assert len(seen) == 1
        seen.clear()
        await link(session_factory, session_id, ids)
    finally:
        stop()
    async with session_factory() as db:
        assert sorted((await db.execute(
            select(Event.data["of"].as_integer()).where(Event.session_id == session_id, Event.type == "many.link")
        )).scalars()) == sorted(ids[:5] + ids)


# -- a table of the tests' own -------------------------------------------------

@pytest_asyncio.fixture(loop_scope="session")
async def table(session_factory):
    """many_rows_cases (id, body, amount), empty, dropped after the test."""
    async with session_factory() as db:
        await db.execute(text("DROP TABLE IF EXISTS many_rows_cases"))
        await db.execute(text("CREATE TABLE many_rows_cases (id integer PRIMARY KEY, body text, amount numeric)"))
        await db.commit()
    yield sqlalchemy.table("many_rows_cases", sqlalchemy.column("id"), sqlalchemy.column("body"), sqlalchemy.column("amount"))
    async with session_factory() as db:
        await db.execute(text("DROP TABLE IF EXISTS many_rows_cases"))
        await db.commit()


async def kept(session_factory) -> int:
    async with session_factory() as db:
        return (await db.execute(text("SELECT count(*) FROM many_rows_cases"))).scalar_one()


BODY = "b" * 40_000


async def _counts(session_factory, table) -> dict[str, int]:
    """What each kind of statement of many rows answers as its rowcount."""
    bound = sqlalchemy.bindparam
    answers = {}
    async with session_factory() as db:
        await db.execute(text("DELETE FROM many_rows_cases"))
        answers["insert"] = (await db.execute(
            sqlalchemy.insert(table), [{"id": n, "body": BODY, "amount": 1} for n in range(3)]
        )).rowcount
        answers["update, three of four match"] = (await db.execute(
            sqlalchemy.update(table).where(table.c.id == bound("which")).values(body=bound("to")),
            [{"which": n, "to": BODY + str(n)} for n in range(4)],
        )).rowcount
        answers["update, a large row then two small"] = (await db.execute(
            sqlalchemy.update(table).where(table.c.id == bound("which")).values(body=bound("to")),
            [{"which": 0, "to": BODY * 2}, {"which": 1, "to": "s"}, {"which": 2, "to": "s"}],
        )).rowcount
        answers["driver sql"] = (await (await db.connection()).exec_driver_sql(
            "INSERT INTO many_rows_cases (id, body) VALUES ($1, $2)", [(n, BODY) for n in range(10, 14)]
        )).rowcount
        answers["few small rows"] = (await db.execute(
            sqlalchemy.insert(table), [{"id": n, "body": "s", "amount": 1} for n in range(20, 23)]
        )).rowcount
        await db.commit()
    return answers


async def test_a_statement_sent_in_groups_answers_the_rowcount_it_answered_whole(session_factory, table, monkeypatch):
    monkeypatch.setattr(many_rows, "GUARD", False)
    whole = await _counts(session_factory, table)
    monkeypatch.setattr(many_rows, "GUARD", True)
    assert await _counts(session_factory, table) == whole


@pytest.mark.parametrize("rows", [2, 5])
async def test_rows_of_3_mib_each_sent_alone_end_when_stopped_at_any_wait(stopping, session_factory, table, rows):
    body = "b" * (3 * 1024 * 1024)

    async def insert():
        async with stopping.session_factory() as db:
            await db.execute(sqlalchemy.insert(table), [{"id": n, "body": body, "amount": 1} for n in range(rows)])
            await db.commit()

    for at in range(200):
        await stopping.stop_at(at, insert())
        left = await kept(session_factory)
        if left == rows:
            break
        assert left == 0
    else:
        pytest.fail("the rows were never inserted")


async def test_a_statement_sent_in_groups_on_an_autocommit_connection_leaves_no_row_when_it_fails(pg_url, session_factory, table):
    """The driver makes one statement of many rows all or nothing by itself; its groups are too."""
    autocommit = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        async with autocommit.connect() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    sqlalchemy.insert(table), [{"id": n, "body": BODY, "amount": 1} for n in (1, 2, 1)]
                )
            assert await kept(session_factory) == 0
            # And one that does not fail is kept, with nothing left open on the connection.
            await connection.execute(sqlalchemy.insert(table), [{"id": n, "body": BODY, "amount": 1} for n in (1, 2, 3)])
            assert await kept(session_factory) == 3
            assert not (await connection.get_raw_connection()).driver_connection.is_in_transaction()
    finally:
        await autocommit.dispose()


async def test_many_rows_of_a_float_whose_decimal_form_is_long_end_when_stopped_at_any_wait(stopping, session_factory, table):
    """A float for a numeric column goes as its exact decimal expansion: 5e-324 is some 390 bytes of it."""
    async def insert():
        async with stopping.session_factory() as db:
            await db.execute(sqlalchemy.insert(table), [{"id": n, "body": None, "amount": 5e-324} for n in range(430)])
            await db.commit()

    for at in range(200):
        await stopping.stop_at(at, insert())
        left = await kept(session_factory)
        if left == 430:
            break
        assert left == 0
    else:
        pytest.fail("the rows were never inserted")
