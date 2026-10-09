"""A statement of many rows is sent so that the driver writes each statement whole.

asyncpg writes an ``executemany`` four packets of 32 KiB or more at a time and
waits between them, inside a ``try`` whose ``finally`` returns: a cancellation
that arrives in that wait is lost, and the call then waits for ever on a
statement it never finished sending, keeping its connection and the rows it
locked.  A connection lost in that wait leaves it waiting too.  Which
statements have many rows, and how large their rows are, is data and the ORM's
own choice (a flush of several changed rows of one table is one), so no list
of statements that are "small enough" holds.

So every engine of this process sends such a statement in groups of rows that
together cannot fill those four packets, each group a statement of its own in
the caller's transaction (on a connection that commits each statement by
itself, in one of the driver's, so that the statement stays all or nothing); a row that could fill them alone goes alone, as a
statement of many rows that has one, which the driver writes whole.  A statement small enough
is sent as it always was.  The listener is on every engine made in a process
that imports ``surogates.db``, the tests' engines too; it acts for asyncpg only.
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.util import await_only

# What fills the driver's first write: _EXECUTE_MANY_BUF_NUM packets of
# _EXECUTE_MANY_BUF_SIZE bytes (asyncpg/protocol/consts.pxi).
_FIRST_WRITE = 4 * 32 * 1024
# Rows whose bounds come to less than this are sent as one statement.  Half the
# first write: the bound below is generous for every value, but not the driver's own count.
WHOLE_BELOW = _FIRST_WRITE // 2
# A row's message beside its values: its header, a statement's name, its counts, and the execute after it.
_ROW = 96
# A value's length and format code, and its bytes when they are few and fixed.
_FIXED = 6 + 16
# Off only in a test that shows a statement safe, or the driver's fault there, without it.
GUARD = True


def _bound(value: Any) -> int | None:
    """No fewer bytes than *value* takes in a row's message; None when that is not known."""
    if value is None or isinstance(value, (bool, float, uuid.UUID, datetime.date, datetime.time, datetime.timedelta)):
        return _FIXED
    if isinstance(value, str):
        return _FIXED + (len(value) if value.isascii() else 4 * len(value))
    if isinstance(value, (bytes, bytearray)):
        return _FIXED + len(value)
    if isinstance(value, memoryview):
        return _FIXED + value.nbytes
    if isinstance(value, int):
        return _FIXED + value.bit_length()
    if isinstance(value, decimal.Decimal):
        return _FIXED + 2 * len(str(value))
    if isinstance(value, (list, tuple)):
        total = _FIXED + 32
        for item in value:
            inner = _bound(item)
            if inner is None:
                return None
            total += inner
        return total
    return None


def row_bound(row: Sequence[Any] | Mapping[str, Any]) -> int | None:
    """No fewer bytes than *row*'s message takes; None when one of its values' sizes is not known."""
    total = _ROW
    for value in row.values() if isinstance(row, Mapping) else row:
        inner = _bound(value)
        if inner is None:
            return None
        total += inner
    return total


def in_groups(rows: Sequence[Any]) -> list[list[Any]]:
    """*rows* in their order, in groups the driver writes whole: each below WHOLE_BELOW together, or one row."""
    groups: list[list[Any]] = []
    group: list[Any] = []
    size = 0
    for row in rows:
        bound = row_bound(row)
        if bound is None or bound >= WHOLE_BELOW:
            if group:
                groups.append(group)
            groups.append([row])
            group, size = [], 0
            continue
        if group and size + bound >= WHOLE_BELOW:
            groups.append(group)
            group, size = [], 0
        group.append(row)
        size += bound
    if group:
        groups.append(group)
    return groups


@event.listens_for(Engine, "do_executemany")
def _send_whole(cursor: Any, statement: str, parameters: Any, context: Any) -> bool | None:
    if not GUARD or context is None or context.dialect.driver != "asyncpg":
        return None
    rows = parameters if isinstance(parameters, (list, tuple)) else list(parameters)
    groups = in_groups(rows)
    if len(groups) == 1 and len(groups[0]) > 1:
        # Small enough: sent as it always was.
        return None
    # Each group as a statement of many rows, a lone row as a list of one: what the
    # cursor then answers (its rowcount, no result) is what the whole statement
    # answered.  One row cannot fill four packets, so the driver writes it whole.
    connection = getattr(cursor, "_adapt_connection", None)
    if getattr(connection, "isolation_level", None) != "autocommit":
        # In the caller's transaction: all of the statement or none of it, as before.
        for group in groups:
            cursor.executemany(statement, group)
        return True
    # No transaction of the caller's: the driver makes one statement of many rows all
    # or nothing by itself, so its groups go in a transaction of the driver's own.
    transaction = connection._connection.transaction()
    await_only(transaction.start())
    try:
        for group in groups:
            cursor.executemany(statement, group)
    except BaseException:
        with contextlib.suppress(Exception):
            await_only(transaction.rollback())
        raise
    await_only(transaction.commit())
    return True
