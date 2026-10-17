"""office_edit's ops, read as the model meant them before any op runs.

A small model's call often says clearly what it means in a form the tool does not take: the ops as JSON text (the
model server's tool parser passes on a list it cannot parse as a string), that text with its last closers missing,
one op without the list, the op's name as a key, or "type" where "op" belongs. Each is read as meant and said in the
result (``read_as``), but one: a list sent as valid JSON text is parsed before the tool runs, by the harness's
argument coercion (``surogates.tools.coerce``), and arrives as a list with nothing said. Anything else is refused with
what is wrong and where. Nothing else is guessed: not quotes, commas or keys, and not a closer missing anywhere but
right before another closer or at the end.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

_REPLY = '{"op": "reply", "thread": "<thread id>", "body": "…"}'
_RESOLVE = '{"op": "resolve", "thread": "<thread id>"}'
_BLOCK = '{"block": "<id>", "hash": "<hash>"}'

#: Each document type's ops, in the order the tool's description gives them, each as one complete op in JSON.
OP_SHAPES: dict[str, dict[str, str]] = {
    "docx": {
        "insert": f'{{"op": "insert", "target": {_BLOCK}, "side": "before|after", "content": {{"markdown": "…"}}, "list": "new"}}',
        "replace": f'{{"op": "replace", "target": {_BLOCK}, "content": {{"markdown": "…"}}}}',
        "delete": f'{{"op": "delete", "target": {_BLOCK}}}',
        "move": f'{{"op": "move", "target": {_BLOCK}, "to": {_BLOCK}, "side": "before|after"}}',
        "comment": '{"op": "comment", "anchor": {"find": "exact words"}, "body": "…"}',
        "reply": _REPLY,
        "resolve": _RESOLVE,
    },
    "xlsx": {
        "set": '{"op": "set", "range": "Sheet1!A1", "values": [["…", 1]]}',
        "format": '{"op": "format", "range": "Sheet1!A1:D1", "number_format": "0.00", "bold": true, "italic": false, '
                  '"fill": "#FFF2CC", "color": "#000000", "align": "left|center|right|justify", '
                  '"valign": "top|center|bottom", "wrap": true, "width": 12}',
        "chart": '{"op": "chart", "range": "Sheet1!A1:B5", "type": "column|bar|line|pie|area|doughnut|scatter", '
                 '"title": "…", "anchor": "F2"}',
        "sheet": '{"op": "sheet", "action": "add|rename|delete", "name": "<sheet>", "to": "<new name>", "after": "<sheet>"}',
        "reply": _REPLY,
        "resolve": _RESOLVE,
    },
    "pptx": {
        "slides": '{"op": "slides", "slides": [{"html": "<section>…</section>", "after": "<slide id>"}]}',
        "text": '{"op": "text", "slide": "<id>", "hash": "<hash>", "shape": "<id>", "text": "…"}',
        "order": '{"op": "order", "ids": ["<slide id>", "…"]}',
        "remove": '{"op": "remove", "slides": [{"id": "<slide id>", "hash": "<hash>"}]}',
        "reply": _REPLY,
        "resolve": _RESOLVE,
    },
    "pdf": {
        "pages": '{"op": "pages", "action": "add|remove|rotate", "page": 1, "degrees": 90}',
        "fill": '{"op": "fill", "fields": {"<field name>": "text, or true/false"}}',
        "add_field": '{"op": "add_field", "type": "text|checkbox", "page": 1, "rect": [72, 72, 288, 96], "name": "<field name>"}',
        "redact": '{"op": "redact", "text": "…"}',
        "annotate": '{"op": "annotate", "kind": "free_text|stamp", "page": 1, "rect": [72, 72, 288, 144], "text": "…"}',
        "reply": _REPLY,
        "resolve": _RESOLVE,
    },
}
#: Each document type's op names.
OP_NAMES: dict[str, tuple[str, ...]] = {kind: tuple(shapes) for kind, shapes in OP_SHAPES.items()}
#: Op names a document type refuses with a reason of its own, as runtime-office refuses them: what to do instead.
REFUSED_OPS: dict[str, dict[str, str]] = {
    "pdf": {
        "replace": "a PDF's existing text cannot be changed: tell the user, and offer what you can do (a note, a "
                   "redaction, a new page).",
    },
}
#: Ops never offered as the nearest to one not known: gone for good once saved, the model names them itself.
_NEVER_GUESSED = frozenset({"redact"})
# The keys each op takes besides "op": an op whose name is missing or unknown is matched to those it shares most of.
_KEYS = {kind: {name: set(json.loads(shape)) - {"op"} for name, shape in shapes.items()} for kind, shapes in OP_SHAPES.items()}
_CLOSERS = {"{": "}", "[": "]"}
# How alike two op names are (difflib's ratio, 0 to 1) to count as near, and how far behind the closest one may be.
_CLOSE = 0.4
_ABOUT_AS_CLOSE = 0.1
_EMPTY = '"ops" is a non-empty list of ops.'
_AS_TEXT = '"ops" was JSON text, not a list: read as the JSON it holds'


class OpsInputError(ValueError):
    """Why the ops cannot be read: said to the model as the tool's error, beside what was read differently from how it
    came on the way (``read_as``, empty when nothing was)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.read_as: list[str] = []


def read_ops(raw: object, kind: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The ops *raw* means for a document of *kind* (``docx``, ``xlsx``, ``pptx`` or ``pdf``), and what was read
    differently from how it came (empty when nothing was). A refusal carries what was read differently before it."""
    read_as: list[str] = []
    try:
        return _read(raw, kind, read_as), read_as
    except OpsInputError as exc:
        exc.read_as = read_as
        raise


def _read(raw: object, kind: str, read_as: list[str]) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        if not raw.strip():
            raise OpsInputError(_EMPTY)
        raw = _parsed(raw, read_as)
    if isinstance(raw, dict) and raw:
        raw = [raw]
        read_as.append('"ops" was one op, not a list: read as a list of one.')
    if not isinstance(raw, list) or not raw:
        raise OpsInputError(_EMPTY)
    ops: list[dict[str, Any]] = []
    for index, op in enumerate(raw):
        if not isinstance(op, dict):
            raise OpsInputError(_unknown(f"op {index} is {_shown(op)}, not an op object", {}, op, kind))
        op, note = _named(op, index, kind)
        if note:
            read_as.append(note)
        if op.get("op") not in OP_NAMES[kind]:
            refused = REFUSED_OPS.get(kind, {}).get(_name_of(op))
            if refused:
                raise OpsInputError(f"op {index}: {refused}")
            if "op" in op:
                raise OpsInputError(_unknown(f'op {index}: {_shown(op["op"])} is not a .{kind} op', op, op["op"], kind))
            said = f' ("type" {_shown(op["type"])} is not a .{kind} op)' if "type" in op else ""
            raise OpsInputError(_unknown(f'op {index} has no "op"{said}', op, op.get("type"), kind))
        ops.append(op)
    return ops


def close_missing(text: str) -> tuple[str, list[str]] | None:
    """*text* with the closers it lacks added, and what was added; None when it fails to parse for any other reason.

    Two failures are taken: the text ends with objects or lists still open (their closers go at the end, innermost
    first), and a closer arrives while a container it does not close is open (the missing closers go right before
    it). A quote, a comma, a stray closer, or a repaired text that still does not parse is not guessed at."""
    open_closers: list[str] = []  # what each open container needs, outermost first
    pieces: list[str] = []
    added: list[str] = []
    start = 0
    in_string = escaped = False
    for at, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in _CLOSERS:
            open_closers.append(_CLOSERS[char])
        elif char in "}]":
            if char not in open_closers:
                return None
            missing = ""
            while open_closers[-1] != char:
                missing += open_closers.pop()
            open_closers.pop()
            if missing:
                pieces += [text[start:at], missing]
                start = at
                added.append(f"missing '{missing}' before the '{char}' at character {at}")
    if in_string:
        return None
    pieces.append(text[start:])
    if open_closers:
        missing = "".join(reversed(open_closers))
        pieces.append(missing)
        added.append(f"missing '{missing}' at the end")
    if not added:
        return None
    fixed = "".join(pieces)
    try:
        json.loads(fixed)
    except (ValueError, RecursionError):
        return None
    return fixed, added


def _parsed(text: str, read_as: list[str]) -> object:
    try:
        parsed = json.loads(text)
    except RecursionError:
        raise OpsInputError('"ops" is nested too deeply.') from None
    except json.JSONDecodeError as exc:
        closed = close_missing(text)
        if closed is None:
            before = text[max(0, exc.pos - 40):exc.pos]
            where = f", just after {before!r}" if before else ""
            # Some of json's messages end in "at" ("Unterminated string starting at"): the place is said once.
            what = exc.msg.removesuffix(" at")
            raise OpsInputError(f"ops is not valid JSON: {what} at character {exc.pos}{where}.") from None
        fixed, added = closed
        read_as.append(f"{_AS_TEXT}, adding the {', the '.join(added)}.")
        return json.loads(fixed)
    read_as.append(f"{_AS_TEXT}.")
    return parsed


def _named(op: dict[str, Any], index: int, kind: str) -> tuple[dict[str, Any], str | None]:
    """*op* with its name under "op" when it came as its only key holding an object, or as "type"; and what was read."""
    if "op" in op:
        return op, None
    if len(op) == 1:
        ((key, value),) = op.items()
        if key in OP_NAMES[kind] and isinstance(value, dict) and value.get("op", key) == key:
            fields = {k: v for k, v in value.items() if k != "op"}
            return {"op": key, **fields}, f'op {index}: read {{"{key}": {{…}}}} as {{"op": "{key}", …}}.'
    name = op.get("type")
    if isinstance(name, str) and name in OP_NAMES[kind]:
        return {"op": name, **{k: v for k, v in op.items() if k != "type"}}, f'op {index}: read "type": "{name}" as "op": "{name}".'
    return op, None


def _name_of(op: dict[str, Any]) -> str | None:
    """The op name *op* gives in any form it is read in: under "op", as its only key holding an object, or as "type"."""
    if "op" in op:
        name = op["op"]
    elif len(op) == 1 and isinstance(next(iter(op.values())), dict):
        name = next(iter(op))
    else:
        name = op.get("type")
    return name if isinstance(name, str) else None


def _unknown(what: str, op: dict[str, Any], name: object, kind: str) -> str:
    names = OP_NAMES[kind]
    message = f"{what}; the .{kind} ops are {', '.join(names[:-1])} or {names[-1]}."
    nearest = _nearest(op, name if isinstance(name, str) else None, kind)
    if nearest:
        message += " The nearest: " + "; ".join(OP_SHAPES[kind][each] for each in nearest)
    return message


def _nearest(op: dict[str, Any], name: str | None, kind: str) -> list[str]:
    """The ops of *kind* nearest *op*, at most three: those sharing the most keys with it, closest by *name* first;
    with no key in common, those whose names are about as close to *name* as the closest. None in _NEVER_GUESSED."""
    names = [each for each in OP_NAMES[kind] if each not in _NEVER_GUESSED]
    keys = set(op) - {"op"}
    shared = {each: len(keys & _KEYS[kind][each]) for each in names}
    alike = {each: difflib.SequenceMatcher(None, name, each).ratio() if name else 0.0 for each in names}
    most = max(shared.values())
    if most:
        nearest = [each for each in names if shared[each] == most]
    else:
        floor = max(_CLOSE, max(alike.values()) - _ABOUT_AS_CLOSE)
        nearest = [each for each in names if alike[each] >= floor]
    return sorted(nearest, key=alike.__getitem__, reverse=True)[:3]


def _shown(value: object) -> str:
    shown = json.dumps(value, ensure_ascii=False)
    return shown if len(shown) <= 80 else shown[:79] + "…"
