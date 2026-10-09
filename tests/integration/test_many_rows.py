"""A statement of many rows on the real database: sent so that a stop at any of its waits ends (surogates.db.many_rows)."""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
import sqlalchemy
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from surogates.db import many_rows

from surogates.db.models import Session, Task, TaskLink

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Each a row that fills one of the driver's packets by itself.
RESULT = "r" * 40_000


_ORGS: list[uuid.UUID] = []


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _its_tasks_go_with_the_test(session_factory):
    """The tasks a test here makes are deleted with it: the dispatcher's tests take up every task they find."""
    yield
    async with session_factory() as db:
        for statement in (
            "DELETE FROM task_links WHERE child_id IN (SELECT id FROM tasks WHERE org_id = ANY(:orgs))",
            "DELETE FROM tasks WHERE org_id = ANY(:orgs)",
        ):
            await db.execute(text(statement), {"orgs": _ORGS})
        await db.commit()
    _ORGS.clear()


async def seed(session_factory, tasks: int) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """A session of a fresh org and *tasks* tasks of it."""
    org_id = await create_org(session_factory)
    _ORGS.append(org_id)
    user_id = await create_user(session_factory, org_id)
    session_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(tasks)]
    async with session_factory() as db:
        db.add(Session(id=session_id, org_id=org_id, user_id=user_id, agent_id="agent-x", config={}))
        await db.commit()
        await db.execute(
            text(
                "INSERT INTO tasks (id, org_id, parent_session_id, goal) "
                "SELECT u, :org, :sess, 'g' FROM unnest(CAST(:ids AS uuid[])) AS u"
            ),
            {"org": org_id, "sess": session_id, "ids": ids},
        )
        await db.commit()
    return org_id, session_id, ids


async def finalize(session_factory, ids: list[uuid.UUID]) -> None:
    """As the dispatcher finalises ended tasks, but for its completed_at: a datetime of
    Python's, with which the ORM sends every task's row in one statement."""
    async with session_factory() as db:
        for task in (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all():
            task.status = "done"
            task.result = RESULT
            task.completed_at = datetime.datetime(2026, 1, 1)
        await db.commit()


async def link(session_factory, org_id, session_id, parents: list[uuid.UUID]) -> uuid.UUID:
    """As create_task_and_spawn records a task and its parents (surogates/tasks/service.py)."""
    async with session_factory() as db:
        task = Task(org_id=org_id, parent_session_id=session_id, goal="g", status="todo")
        db.add(task)
        await db.flush()
        for parent in parents:
            db.add(TaskLink(parent_id=parent, child_id=task.id))
        await db.commit()
        return task.id


async def done(session_factory, ids) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(Task).where(Task.id.in_(ids), Task.status == "done")
        )).scalar_one()


async def links(session_factory, parents) -> int:
    async with session_factory() as db:
        return (await db.execute(
            select(func.count()).select_from(TaskLink).where(TaskLink.parent_id.in_(parents))
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
    _, _, ids = await seed(session_factory, 6)
    for at in range(200):
        await stopping.stop_at(at, finalize(stopping.session_factory, ids))
        changed = await done(session_factory, ids)
        if changed == 6:
            break
        # Stopped before its commit: none of them.
        assert changed == 0
    else:
        pytest.fail("the tasks were never finalised")
    async with session_factory() as db:
        assert set((await db.execute(select(Task.result).where(Task.id.in_(ids)))).scalars()) == {RESULT}


async def test_the_same_flush_never_ends_when_stopped_without_the_guard(stopping, session_factory, unguarded):
    """The driver's own fault, kept in sight: the day this fails, asyncpg ends such a call and the guard may go."""
    _, _, ids = await seed(session_factory, 6)
    with pytest.raises(AssertionError, match="the call did not end"):
        for at in range(200):
            await stopping.stop_at(at, finalize(stopping.session_factory, ids), within=3.0)
            if await done(session_factory, ids) == 6:
                break


@pytest.mark.parametrize("parents", [5, 2_600])
async def test_a_task_with_many_parents_has_every_link_or_none_when_stopped_at_any_wait(stopping, session_factory, parents):
    org_id, session_id, ids = await seed(session_factory, parents)
    for at in range(400):
        stopped = await stopping.stop_at(at, link(stopping.session_factory, org_id, session_id, ids))
        made = await links(session_factory, ids)
        if made == parents:
            break
        assert made == 0 and stopped
    else:
        pytest.fail("the task was never linked")


async def test_few_small_rows_go_as_one_statement_and_many_in_groups_all_of_them_once(engine, session_factory):
    org_id, session_id, ids = await seed(session_factory, 2_600)
    seen, stop = statements(engine, "task_links")
    try:
        await link(session_factory, org_id, session_id, ids[:5])
        # As it always was: one statement of the five rows.
        assert seen == [(True, 5)]
        seen.clear()
        child = await link(session_factory, org_id, session_id, ids)
    finally:
        stop()
    async with session_factory() as db:
        assert sorted((await db.execute(
            select(TaskLink.parent_id).where(TaskLink.child_id == child)
        )).scalars()) == sorted(ids)


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
