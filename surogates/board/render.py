"""Windowed board render + delta render.

Pure functions over note rows (ORM objects or any duck-typed object with
the BoardNote attributes) so they are trivially unit-testable.  All
filtering/dedup/priority logic lives here; the store only fetches rows.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from surogates.board.types import (
    CHARS_PER_TOKEN,
    FAIL_RESERVE_FRACTION,
    RENDER_PRIORITY,
    STATUS_ACTIVE,
)

_WS_RE = re.compile(r"\s+")

_SNAPSHOT_HEADER = (
    "[Shared board — verified notes from your coordination group]"
)
_SNAPSHOT_FOOTER = (
    "Later [Board update] messages supersede earlier ones; call read_board "
    "for the consolidated current state."
)


def _norm(content: str) -> str:
    return _WS_RE.sub(" ", content).strip().lower()


def _age(now: datetime, ts: datetime) -> str:
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return f"+{secs}s"
    if secs < 3600:
        return f"+{secs // 60}m"
    if secs < 86400:
        return f"+{secs // 3600}h"
    return f"+{secs // 86400}d"


def _ttl_left(now: datetime, expires_at: datetime) -> str:
    secs = int((expires_at - now).total_seconds())
    if secs <= 0:
        return "expired"
    if secs < 60:
        return f"{secs}s left"
    return f"{secs // 60}m left"


def note_line(note: Any, *, now: datetime) -> str:
    """One render line: ``[n42 w3f2/FAIL +2m] content``."""
    parts = f"[n{note.id} {note.writer_label}/{note.type} {_age(now, note.created_at)}"
    if note.type == "CLAIM" and note.expires_at is not None:
        parts += f", {_ttl_left(now, note.expires_at)}"
    return f"{parts}] {note.content}"


def visible_notes(notes: Iterable[Any], *, now: datetime) -> list[Any]:
    """Active notes minus lapsed claims, exact-deduped keeping newest."""
    live = [
        n for n in notes
        if n.status == STATUS_ACTIVE
        and not (n.type == "CLAIM" and n.expires_at is not None
                 and n.expires_at <= now)
    ]
    by_key: dict[tuple[str, str], Any] = {}
    for n in live:
        key = (n.type, _norm(n.content))
        prev = by_key.get(key)
        if prev is None or n.created_at > prev.created_at:
            by_key[key] = n
    return list(by_key.values())


def _ordered(notes: list[Any]) -> list[Any]:
    return sorted(
        notes,
        key=lambda n: (RENDER_PRIORITY.get(n.type, 99), -n.created_at.timestamp()),
    )


def render_board(
    notes: Iterable[Any],
    *,
    max_tokens: int,
    now: datetime,
    header: str = _SNAPSHOT_HEADER,
    footer: str = _SNAPSHOT_FOOTER,
) -> str:
    """Budgeted snapshot render of the board's current visible state.

    35% of the char budget is reserved for FAIL notes (newest first) so
    dead ends never scroll out; the remainder is filled in priority
    order.  Returns "" for an empty board.
    """
    visible = _ordered(visible_notes(notes, now=now))
    if not visible:
        return ""

    budget = max_tokens * CHARS_PER_TOKEN
    fail_reserve = int(budget * FAIL_RESERVE_FRACTION)

    fails = [n for n in visible if n.type == "FAIL"]
    selected: set[int] = set()
    used = 0
    for n in fails:
        line_len = len(note_line(n, now=now)) + 1
        if used + line_len > fail_reserve:
            break
        selected.add(n.id)
        used += line_len

    for n in visible:
        if n.id in selected:
            continue
        line_len = len(note_line(n, now=now)) + 1
        if used + line_len > budget:
            continue
        selected.add(n.id)
        used += line_len

    lines = [note_line(n, now=now) for n in visible if n.id in selected]
    omitted = len(visible) - len(lines)
    if omitted > 0:
        lines.append(f"… +{omitted} more — call read_board")
    if footer:
        return f"{header}\n" + "\n".join(lines) + f"\n{footer}"
    return f"{header}\n" + "\n".join(lines)


def render_delta(
    changed: Iterable[Any],
    *,
    max_chars: int,
    now: datetime,
) -> str:
    """Compact render of notes whose ``seq`` moved past the cursor.

    Classifies each row as new / renewed claim / status transition.
    Newest first; overflow drops oldest with a read_board pointer.
    Returns "" when nothing changed.
    """
    rows = sorted(changed, key=lambda n: -n.seq)
    if not rows:
        return ""

    lines: list[str] = []
    for n in rows:
        if n.status == STATUS_ACTIVE:
            if n.type == "CLAIM" and n.updated_at > n.created_at:
                lines.append(f"renewed: {note_line(n, now=now)}")
            else:
                lines.append(note_line(n, now=now))
        else:
            lines.append(f"{n.status}: {note_line(n, now=now)}")

    header = "[Board update]"
    kept: list[str] = []
    used = len(header) + 1
    omitted = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            omitted += 1
            continue
        kept.append(line)
        used += len(line) + 1
    if omitted > 0:
        kept.append(f"… +{omitted} more — call read_board")
    return header + "\n" + "\n".join(kept)
