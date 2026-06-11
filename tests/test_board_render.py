"""Windowed board render + delta render (pure functions)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from surogates.board.render import render_board, render_delta

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeNote:
    id: int
    seq: int
    type: str
    content: str
    writer_label: str = "w1aa"
    status: str = "active"
    expires_at: datetime | None = None
    created_at: datetime = field(default=NOW - timedelta(minutes=2))
    updated_at: datetime = field(default=NOW - timedelta(minutes=2))
    ref: dict[str, Any] | None = None


def _mk(i, type_, content, **kw):
    return FakeNote(id=i, seq=i, type=type_, content=content, **kw)


def test_render_orders_by_priority_then_recency():
    notes = [
        _mk(1, "CLAIM", "claiming telegram refactor",
            expires_at=NOW + timedelta(minutes=4)),
        _mk(2, "FAIL", "socket-mode tests blocked: egress denied"),
        _mk(3, "FACT", "slack adapter bypasses outbox for DMs"),
        _mk(4, "RESULT", "outcome=slack done|evidence=14/14 passed|risk=none"),
    ]
    text = render_board(notes, max_tokens=600, now=NOW)
    pos = {t: text.index(t) for t in ("RESULT", "FACT", "FAIL", "CLAIM")}
    assert pos["RESULT"] < pos["FACT"] < pos["FAIL"] < pos["CLAIM"]
    assert "[n4 w1aa/RESULT +2m]" in text


def test_render_filters_expired_claims_and_non_active():
    notes = [
        _mk(1, "CLAIM", "stale claim", expires_at=NOW - timedelta(seconds=1)),
        _mk(2, "RESULT", "outcome=old|evidence=ran|risk=-", status="superseded"),
        _mk(3, "FACT", "the only live note"),
    ]
    text = render_board(notes, max_tokens=600, now=NOW)
    assert "stale claim" not in text
    assert "outcome=old" not in text
    assert "the only live note" in text


def test_render_dedupes_exact_content_keeping_newest():
    old = _mk(1, "FACT", "duplicate fact",
              created_at=NOW - timedelta(minutes=30))
    new = _mk(2, "FACT", "Duplicate   fact")  # normalization: case+whitespace
    text = render_board([old, new], max_tokens=600, now=NOW)
    assert text.count("uplicate") == 1
    assert "[n2" in text


def test_render_fail_reserve_keeps_fails_under_pressure():
    facts = [
        _mk(i, "FACT", f"fact number {i} " + "x" * 150) for i in range(1, 30)
    ]
    fails = [
        _mk(100 + i, "FAIL", f"dead end {i} " + "y" * 120) for i in range(3)
    ]
    text = render_board(facts + fails, max_tokens=300, now=NOW)
    # All three FAILs survive in the 35% reserve even though FACTs
    # outrank them and would otherwise exhaust the window.
    assert all(f"dead end {i}" in text for i in range(3))
    assert "more — call read_board" in text


def test_render_empty_returns_empty_string():
    assert render_board([], max_tokens=600, now=NOW) == ""


def test_delta_classifies_new_changed_and_caps():
    new_note = _mk(10, "FACT", "fresh discovery")
    superseded = _mk(3, "RESULT", "outcome=v1|evidence=ran|risk=-",
                     status="superseded")
    renewed = FakeNote(
        id=7, seq=11, type="CLAIM", content="claiming auth module",
        expires_at=NOW + timedelta(minutes=5),
        created_at=NOW - timedelta(minutes=9),
        updated_at=NOW - timedelta(seconds=5),
    )
    text = render_delta([new_note, superseded, renewed],
                        max_chars=1200, now=NOW)
    assert "[Board update]" in text
    assert "fresh discovery" in text
    assert "superseded" in text
    assert "renewed" in text


def test_delta_overflow_points_to_read_board():
    notes = [_mk(i, "FACT", f"note {i} " + "z" * 180) for i in range(1, 40)]
    text = render_delta(notes, max_chars=400, now=NOW)
    assert len(text) <= 400 + 80  # wrapper line tolerance
    assert "more — call read_board" in text
