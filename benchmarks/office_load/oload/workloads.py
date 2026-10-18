"""What a virtual session works on: its document, picked by the mix, and the one-op edits it makes, each writing a
marker that the save is checked for."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

# The document mix, as the office benchmark's: each kind's share of the sessions.
KIND_SHARES: dict[str, float] = {"docx": 0.35, "xlsx": 0.25, "pptx": 0.30, "pdf": 0.10}

# runtime-office's own test documents: every session opens a copy of its kind's sample.
FIXTURES = Path(__file__).resolve().parents[3] / "runtime-office" / "test" / "fixtures"

# One word that Markdown, a slide's text box, a spreadsheet cell and a PDF note all keep as it is: starting with a
# letter, it is never read as a number, a date, a formula or a list.
MARKER = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")

# Where the edits land in the samples. sample.xlsx's first sheet fills A1:D4. sample.pdf's page 1 is A4 (595 x 842
# points) and its text and fields end about 230 points from the top: notes go below, in 13 rows that repeat.
# sample.pptx's slide 2 ("Next quarter") has a title and one text box, whose text each deck edit replaces.
SHEET = "Sales"
FREE_COLUMN = "E"
NOTE_LEFT, NOTE_RIGHT = 72, 360
NOTE_TOP, NOTE_HEIGHT, NOTE_STEP, NOTE_ROWS = 260, 30, 40, 13
DECK_SLIDE = 2

# A deck's ids are the editor's own, and a slide's change is guarded by the hash its last read gave: the outline is
# read once, for the slide's id, and the slide before each edit (slide_read), as an agent reads before it edits.
OUTLINE_READ: dict[str, Any] = {"view": "outline"}


class DeckChanged(ValueError):
    """The deck's read does not show the slide or the text box the edits change."""


def _spread(weights: dict[str, int]) -> tuple[str, ...]:
    """Each kind as many times as its weight, spread evenly through the cycle (smooth weighted round robin)."""
    total = sum(weights.values())
    credit = dict.fromkeys(weights, 0)
    cycle = []
    for _ in range(total):
        for kind, weight in weights.items():
            credit[kind] += weight
        kind = max(credit, key=credit.__getitem__)
        credit[kind] -= total
        cycle.append(kind)
    return tuple(cycle)


# Twenty sessions in a row hold the mix exactly; the first six hold every kind.
_CYCLE = _spread({kind: round(share * 20) for kind, share in KIND_SHARES.items()})


def pick_kind(i: int) -> str:
    """Session *i*'s kind."""
    return _CYCLE[i % len(_CYCLE)]


def _kind(kind: str) -> str:
    if kind not in KIND_SHARES:
        raise ValueError(f"the tool opens {', '.join(KIND_SHARES)}, not {kind!r}")
    return kind


def slide_read(outline: dict[str, Any]) -> dict[str, Any]:
    """The read before each deck edit: slide DECK_SLIDE's view, by its id in *outline* (the OUTLINE_READ's answer)."""
    for slide in outline.get("slides") or []:
        if slide.get("number") == DECK_SLIDE:
            return {"view": "slide", "target": str(slide["id"])}
    raise DeckChanged(f"the deck's outline has no slide {DECK_SLIDE}: {str(outline)[:300]}")


def _text_op(read: dict[str, Any], marker: str) -> dict[str, Any]:
    if "error" in read:
        raise DeckChanged(f"the slide could not be read: {read['error']}")
    for shape in read.get("shapes") or []:
        # The text box, not the title: a shape with paragraphs that is no placeholder.
        if shape.get("kind") == "shape" and "paras" in shape and "placeholder" not in shape:
            return {"op": "text", "slide": str(read["id"]), "hash": str(read["hash"]), "shape": str(shape["id"]),
                    "text": marker}
    raise DeckChanged(f"slide {read.get('id')} has no text box: {str(read)[:300]}")


@cache
def fixture(kind: str) -> bytes:
    """The sample document a *kind* session opens."""
    return (FIXTURES / f"sample.{_kind(kind)}").read_bytes()


def edit_ops(kind: str, marker: str, n: int, read: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The ops of one office_edit call on a *kind* sample: a single op, of the shape agents send, that writes *marker*.

    *n* is the edit's number in the session, from 0: each spreadsheet edit takes a cell of its own, each PDF note a
    row of its own. The shapes are runtime-office's, as skills/productivity/office/references/<kind>.md gives them.

    A Word paragraph goes at the end, a cell in its free column and a note by its rect, with no read first. A deck edit
    replaces the text box's text on slide DECK_SLIDE, and *read* is that slide's view, read just before (slide_read);
    no other kind takes one. A read that is not the sample's slide raises DeckChanged.
    """
    _kind(kind)
    if read is not None and kind != "pptx":
        raise ValueError(f"only a deck edit takes a read, not a {kind} edit")
    if not MARKER.fullmatch(marker):
        raise ValueError(f"a marker is one word of letters, digits and hyphens, starting with a letter and at most 64 "
                         f"characters long, not {marker!r}")
    if n < 0:
        raise ValueError(f"n is the edit's number in the session, from 0, not {n}")
    if kind == "docx":
        return [{"op": "insert", "target": {"end": True}, "content": {"markdown": marker}}]
    if kind == "xlsx":
        return [{"op": "set", "range": f"{SHEET}!{FREE_COLUMN}{n + 2}", "values": [[marker]]}]
    if kind == "pptx":
        if read is None:
            raise ValueError("a deck edit needs the read of its slide just before it (slide_read)")
        return [_text_op(read, marker)]
    top = NOTE_TOP + (n % NOTE_ROWS) * NOTE_STEP
    return [{"op": "annotate", "kind": "free_text", "page": 1, "rect": [NOTE_LEFT, top, NOTE_RIGHT, top + NOTE_HEIGHT],
             "text": marker}]
