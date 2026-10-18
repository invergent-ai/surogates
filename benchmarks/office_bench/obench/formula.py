"""A small spreadsheet formula evaluator, for the checks.

The checks must know what a formula computes, not only that a cell starts
with ``=``: a total that sums the wrong range is the failure worth finding.
A file may store each formula's last computed value, but a document saved
without recalculation stores none or a stale one, so the checks evaluate
formulas themselves and compare with the stored value where there is one.

Covers what office tasks write: numbers, percentages, cell references
(``A1``, ``$B$2``, ``Sheet!A1``, ``'Q3 plan'!A1``), ranges inside the
aggregate functions, ``+ - * / ^ &``, parentheses, unary minus, SUM,
AVERAGE, MIN, MAX, COUNT, ROUND, ABS, PRODUCT, SUMPRODUCT and IF with
comparisons, and the dates of DATE, DAYS, DATEDIF in days and NETWORKDAYS
(a date is its day number). Anything else raises :class:`Unsupported`, which
a check reports as "cannot evaluate" rather than as a failure of the agent.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from typing import Any

_EPOCH = _dt.datetime(1899, 12, 30)


class Unsupported(Exception):
    """The formula uses something this evaluator does not implement."""


class FormulaError(Exception):
    """The formula is broken as written: a reference to text, a division by zero, a cycle."""


_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?)
  | (?P<num>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?%?|\.\d+%?)
  | (?P<func>[A-Za-z][A-Za-z0-9.]*)\s*\(
  | (?P<bool>TRUE|FALSE)\b
  | (?P<op><=|>=|<>|[-+*/^&=<>(),:;])
    """,
    re.VERBOSE | re.IGNORECASE,
)


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def split_ref(ref: str, default_sheet: str) -> tuple[str, str]:
    """``'Q3 plan'!$A$1:B2`` -> (``Q3 plan``, ``A1:B2``)."""
    if "!" in ref:
        sheet, cells = ref.rsplit("!", 1)
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    else:
        sheet, cells = default_sheet, ref
    return sheet, cells.replace("$", "").upper()


def expand_range(cells: str) -> list[str]:
    if ":" not in cells:
        return [cells]
    a, b = cells.split(":")
    ma = re.match(r"([A-Z]+)(\d+)$", a)
    mb = re.match(r"([A-Z]+)(\d+)$", b)
    if not ma or not mb:
        raise Unsupported(f"range {cells}")
    c1, c2 = sorted((col_index(ma.group(1)), col_index(mb.group(1))))
    r1, r2 = sorted((int(ma.group(2)), int(mb.group(2))))
    if (c2 - c1 + 1) * (r2 - r1 + 1) > 100_000:
        raise Unsupported(f"range {cells} too large")
    return [f"{col_letters(c)}{r}" for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def to_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, _dt.datetime):
        return (value - _EPOCH).total_seconds() / 86400.0
    if isinstance(value, _dt.date):
        return float((_dt.datetime.combine(value, _dt.time()) - _EPOCH).days)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError:
            raise FormulaError(f"text {value!r} used as a number") from None
    raise FormulaError(f"cannot use {type(value).__name__} as a number")


Getter = Callable[[str, str], Any]  # (sheet, cell) -> raw cell content (formula string or value)


class Evaluator:
    """Evaluates formulas over a workbook given as a getter of raw cell content."""

    def __init__(self, get_raw: Getter) -> None:
        self._get_raw = get_raw
        self._cache: dict[tuple[str, str], Any] = {}
        self._active: set[tuple[str, str]] = set()

    def cell(self, sheet: str, ref: str) -> Any:
        key = (sheet, ref.replace("$", "").upper())
        if key in self._cache:
            return self._cache[key]
        if key in self._active:
            raise FormulaError(f"circular reference at {sheet}!{key[1]}")
        raw = self._get_raw(*key)
        if isinstance(raw, str) and raw.startswith("="):
            self._active.add(key)
            try:
                value = self.formula(raw, sheet)
            finally:
                self._active.discard(key)
        else:
            value = raw
        self._cache[key] = value
        return value

    def formula(self, text: str, sheet: str) -> Any:
        tokens = _tokenize(text[1:] if text.startswith("=") else text)
        parser = _Parser(tokens, self, sheet)
        value = parser.expr()
        if parser.pos != len(tokens):
            raise Unsupported(f"unparsed tail in {text!r}")
        return value


def _tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise Unsupported(f"cannot read {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        value = m.group("func") if kind == "func" else m.group(kind)
        out.append((kind, value))
    return out


class _Range(list):
    """The values of a range, kept apart from a scalar for the aggregates."""


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], ev: Evaluator, sheet: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.ev = ev
        self.sheet = sheet

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, op: str) -> None:
        tok = self.peek()
        if tok is None or tok[1] != op:
            raise Unsupported(f"expected {op!r}")
        self.pos += 1

    def expr(self) -> Any:
        left = self.concat()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in ("=", "<>", "<", ">", "<=", ">="):
            self.take()
            right = self.concat()
            a, b = _scalar(left), _scalar(right)
            if not isinstance(a, str) and not isinstance(b, str):
                a, b = to_number(a), to_number(b)
            left = {
                "=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
            }[tok[1]]
        return left

    def concat(self) -> Any:
        left = self.additive()
        while (tok := self.peek()) and tok == ("op", "&"):
            self.take()
            right = self.additive()
            left = f"{_text(_scalar(left))}{_text(_scalar(right))}"
        return left

    def additive(self) -> Any:
        left = self.term()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in "+-" and len(tok[1]) == 1:
            self.take()
            right = self.term()
            a, b = to_number(_scalar(left)), to_number(_scalar(right))
            left = a + b if tok[1] == "+" else a - b
        return left

    def term(self) -> Any:
        left = self.power()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in ("*", "/"):
            self.take()
            right = self.power()
            a, b = to_number(_scalar(left)), to_number(_scalar(right))
            if tok[1] == "/":
                if b == 0:
                    raise FormulaError("division by zero")
                left = a / b
            else:
                left = a * b
        return left

    def power(self) -> Any:
        left = self.unary()
        while (tok := self.peek()) and tok == ("op", "^"):
            self.take()
            right = self.unary()
            left = to_number(_scalar(left)) ** to_number(_scalar(right))
        return left

    def unary(self) -> Any:
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("-", "+"):
            self.take()
            value = to_number(_scalar(self.unary()))
            return -value if tok[1] == "-" else value
        return self.atom()

    def atom(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise Unsupported("formula ends early")
        kind, value = self.take()
        if kind == "num":
            return float(value[:-1]) / 100 if value.endswith("%") else float(value)
        if kind == "str":
            return value[1:-1].replace('""', '"')
        if kind == "bool":
            return value.upper() == "TRUE"
        if kind == "ref":
            sheet, cells = split_ref(value, self.sheet)
            if ":" in cells:
                return _Range(self.ev.cell(sheet, c) for c in expand_range(cells))
            return self.ev.cell(sheet, cells)
        if kind == "func":
            return self.call(value.upper())
        if (kind, value) == ("op", "("):
            inner = self.expr()
            self.expect(")")
            return inner
        raise Unsupported(f"unexpected {value!r}")

    def args(self) -> list[Any]:
        out: list[Any] = []
        if self.peek() == ("op", ")"):
            self.take()
            return out
        while True:
            out.append(self.expr())
            tok = self.take()
            if tok[1] == ")":
                return out
            if tok[1] not in (",", ";"):
                raise Unsupported(f"unexpected {tok[1]!r} in arguments")

    def call(self, name: str) -> Any:
        args = self.args()
        flat = [v for a in args for v in (a if isinstance(a, _Range) else [a])]

        def numbers(skip_text_in_ranges: bool = True) -> list[float]:
            vals: list[float] = []
            for a in args:
                if isinstance(a, _Range):
                    vals.extend(
                        to_number(v) for v in a
                        if v not in (None, "") and not (skip_text_in_ranges and isinstance(v, str))
                    )
                else:
                    vals.append(to_number(a))
            return vals

        if name == "SUM":
            return sum(numbers())
        if name == "AVERAGE":
            vals = numbers()
            if not vals:
                raise FormulaError("AVERAGE of nothing")
            return sum(vals) / len(vals)
        if name == "MIN":
            return min(numbers() or [0.0])
        if name == "MAX":
            return max(numbers() or [0.0])
        if name == "COUNT":
            return float(sum(1 for v in flat if isinstance(v, (int, float)) and not isinstance(v, bool)))
        if name == "COUNTA":
            return float(sum(1 for v in flat if v not in (None, "")))
        if name == "PRODUCT":
            out = 1.0
            for v in numbers():
                out *= v
            return out
        if name == "ABS":
            return abs(to_number(_scalar(args[0])))
        if name in ("ROUND", "ROUNDUP", "ROUNDDOWN"):
            digits = int(to_number(_scalar(args[1]))) if len(args) > 1 else 0
            return round(to_number(_scalar(args[0])), digits)
        if name == "IF":
            cond = _scalar(args[0])
            if len(args) < 2:
                raise Unsupported("IF without a value")
            if cond if isinstance(cond, bool) else to_number(cond) != 0:
                return _scalar(args[1])
            return _scalar(args[2]) if len(args) > 2 else False
        if name == "DATE":
            if len(args) != 3:
                raise Unsupported("DATE without a year, a month and a day")
            year, month, day = (int(to_number(_scalar(a))) for a in args)
            # As a spreadsheet reads them: a year under 1900 counts from 1900, a month past 12 runs into the next year,
            # and a day past the month's end into the next month.
            year = year + 1900 if 0 <= year < 1900 else year
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            try:
                return to_number(_dt.datetime(year, month, 1)) + day - 1
            except ValueError:
                raise FormulaError(f"DATE({year}, {month}, {day}) is no date") from None
        if name == "DAYS":
            return to_number(_scalar(args[0])) - to_number(_scalar(args[1]))
        if name == "DATEDIF":
            unit = str(_scalar(args[2])).upper() if len(args) > 2 else "D"
            if unit != "D":
                raise Unsupported(f"DATEDIF unit {unit}")
            return to_number(_scalar(args[1])) - to_number(_scalar(args[0]))
        if name == "NETWORKDAYS":
            a, b = int(to_number(_scalar(args[0]))), int(to_number(_scalar(args[1])))
            lo, hi = min(a, b), max(a, b)
            count = sum(1 for d in range(lo, hi + 1) if (_EPOCH + _dt.timedelta(days=d)).weekday() < 5)
            return float(count if b >= a else -count)
        if name == "SUMPRODUCT":
            ranges = [a for a in args if isinstance(a, _Range)]
            if len(ranges) != len(args) or len({len(r) for r in ranges}) != 1:
                raise Unsupported("SUMPRODUCT of mismatched ranges")
            total = 0.0
            for values in zip(*ranges):
                prod = 1.0
                for v in values:
                    prod *= to_number(v)
                total += prod
            return total
        raise Unsupported(f"function {name}")


def _scalar(value: Any) -> Any:
    if isinstance(value, _Range):
        if len(value) == 1:
            return value[0]
        raise Unsupported("a range where one value is meant")
    return value


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)
