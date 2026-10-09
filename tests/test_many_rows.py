"""How a statement of many rows is cut into statements the driver writes whole (surogates.db.many_rows)."""

from __future__ import annotations

import datetime
import decimal
import enum
import importlib
import subprocess
import sys
import uuid

from sqlalchemy import event
from sqlalchemy.engine import Engine

from surogates.db import many_rows
from surogates.db.many_rows import WHOLE_BELOW, in_groups, row_bound


def test_rows_that_cannot_fill_the_drivers_first_write_stay_one_statement():
    rows = [(uuid.uuid4(), uuid.uuid4()) for _ in range(50)]
    assert in_groups(rows) == [rows]


def test_more_rows_go_in_groups_each_below_the_bound_in_their_order():
    rows = [(uuid.uuid4(), n) for n in range(5_000)]
    groups = in_groups(rows)
    assert len(groups) > 1
    assert [row for group in groups for row in group] == rows
    assert all(sum(row_bound(row) for row in group) < WHOLE_BELOW for group in groups)


def test_a_row_that_could_fill_it_alone_goes_alone():
    small, large = ("a", 1), ("x" * WHOLE_BELOW, 2)
    assert in_groups([small, small, large, small, large, large]) == [[small, small], [large], [small], [large], [large]]


def test_a_value_whose_size_is_not_known_sends_its_row_alone():
    class Odd:
        pass

    odd = (Odd(),)
    assert row_bound(odd) is None
    assert in_groups([("a",), odd, ("b",)]) == [[("a",)], [odd], [("b",)]]


def _numeric_bytes(number: decimal.Decimal) -> int:
    """No less than a numeric takes in the driver's binary form: a header, and two bytes for every four
    decimal digits it has, with the zeros a positive exponent stands for. Zeros after the point are not sent."""
    _, digits, exponent = number.as_tuple()
    if not isinstance(exponent, int):
        return 8
    return 8 + 2 * ((len(digits) + max(exponent, 0)) // 4 + 2)


def test_the_bound_is_never_under_what_a_value_takes_on_the_wire():
    assert row_bound(("é" * 10,)) >= len(("é" * 10).encode()) and row_bound(("a" * 10,)) < row_bound(("é" * 10,))
    data = b"x" * 1000
    assert row_bound((data,)) >= 1000 and row_bound((memoryview(data),)) >= 1000 and row_bound((bytearray(data),)) >= 1000
    assert row_bound((None, True, 7, uuid.uuid4(), datetime.datetime.now(), datetime.date.today())) < 400
    # A float for a numeric column is its exact decimal expansion: the smallest one is the longest.
    assert row_bound((5e-324,)) >= _numeric_bytes(decimal.Decimal(5e-324)) and row_bound((1.5,)) == row_bound((5e-324,))
    for number in (decimal.Decimal("1" * 300), decimal.Decimal("1E+1000"), decimal.Decimal("1E-1000"), decimal.Decimal("NaN")):
        assert row_bound((number,)) >= _numeric_bytes(number), number
    assert row_bound((10 ** 400,)) >= _numeric_bytes(decimal.Decimal(10 ** 400))

    class Name(str):
        pass

    class Colour(str, enum.Enum):
        RED = "é" * 10

    assert row_bound((Name("é" * 10),)) == row_bound(("é" * 10,)) == row_bound((Colour.RED,))
    assert row_bound((enum.Enum("Plain", "A").A,)) is None
    assert row_bound((["x" * 500, "y" * 500],)) >= 1000
    assert row_bound({"a": "x" * 500}) >= 500  # named parameters


def test_the_guard_is_on_every_engine_of_a_process_that_imports_the_database_layer():
    assert many_rows.GUARD is True
    assert event.contains(Engine, "do_executemany", many_rows._send_whole)
    # The worker makes its own engine, and the ops' store its own: importing either brings the guard.
    for module in ("surogates.orchestrator.worker", "surogates.db.ops_engine", "surogates.db.engine"):
        code = f"import {module}; from sqlalchemy import event; from sqlalchemy.engine import Engine; import sys; m = sys.modules['surogates.db.many_rows']; assert event.contains(Engine, 'do_executemany', m._send_whole)"
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=120)
