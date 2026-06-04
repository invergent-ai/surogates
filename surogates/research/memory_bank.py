"""Pure logic for the deep-research evidence bank.

An evidence bank is an ordered list of :class:`MemoryEntry` records, each
a curated, pre-summarized source the writer agent later cites by its
stable ``source_id`` (``S1``, ``S2``, ...).  This module is IO-free:
callers load the JSONL from the shared workspace, mutate the list
in-place, and serialize it back.

The bank lives in the parent planner's session workspace at
``{workspace_path}/.research/memory.jsonl``.  Because surogates
sub-agent sessions inherit the same workspace, the writer reads the
same file the planner wrote.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "MemoryEntry",
    "add_entry",
    "parse_jsonl",
    "retrieve",
    "serialize_jsonl",
]


# Matches alpha-numeric runs in lowercase text.  Used for the simple
# keyword-overlap scorer.  Punctuation, whitespace and accents do not
# need to round-trip — the scorer is intentionally token-set-based.
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class MemoryEntry:
    """One curated source in the evidence bank."""

    source_id: str
    url: str
    title: str
    summary: str
    evidence: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def add_entry(
    entries: list[MemoryEntry],
    *,
    url: str,
    title: str,
    summary: str,
    evidence: list[str] | None = None,
) -> MemoryEntry:
    """Append a new entry, or return the existing one for a duplicate URL.

    Source IDs are assigned sequentially as ``S{n}`` where ``n = len(entries) + 1``
    so they remain stable for the lifetime of a research run; a partial
    write of the JSONL still resumes with the next available ID on the
    next call because the existing IDs round-trip through ``parse_jsonl``.

    Dedup is by URL only: the planner may legitimately re-summarize a
    source with new wording, but the writer must cite the same ID.
    """

    for existing in entries:
        if existing.url == url:
            return existing

    entry = MemoryEntry(
        source_id=f"S{len(entries) + 1}",
        url=url,
        title=title,
        summary=summary,
        evidence=list(evidence or []),
    )
    entries.append(entry)
    return entry


def retrieve(
    entries: list[MemoryEntry],
    *,
    query: str,
    k: int = 5,
) -> list[MemoryEntry]:
    """Return up to *k* entries ranked by keyword overlap with *query*.

    Scoring is deliberately simple (token-set overlap over title +
    summary + evidence): it keeps the writer's per-section retrieval
    cheap and model-independent.  Ties break toward earlier (more
    established) sources so the writer's section order stays stable
    across re-runs.

    An empty query is a "give me everything" request used by the
    writer to enumerate the bank for the References section; in that
    case the first ``k`` entries are returned in insertion order.
    """

    q = _tokens(query)
    if not q:
        return entries[:k]

    scored: list[tuple[int, int, MemoryEntry]] = []
    for idx, e in enumerate(entries):
        haystack = _tokens(" ".join([e.title, e.summary, *e.evidence]))
        score = len(q & haystack)
        if score > 0:
            # Negate idx so a higher tuple compares "better" (older
            # entry wins) when scores tie under the descending sort.
            scored.append((score, -idx, e))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [e for _, _, e in scored[:k]]


def serialize_jsonl(entries: list[MemoryEntry]) -> str:
    """Serialize the bank as newline-delimited JSON.

    UTF-8 characters are preserved verbatim (``ensure_ascii=False``)
    so a non-English title round-trips byte-for-byte.
    """

    return "".join(
        json.dumps(asdict(e), ensure_ascii=False) + "\n"
        for e in entries
    )


def _coerce_evidence(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value]


def parse_jsonl(text: str) -> list[MemoryEntry]:
    """Parse a JSONL bank, skipping blank or malformed lines.

    Defensive against partial writes and hand-edits — see the unit
    tests for the exact tolerances.  A field that is missing or of
    the wrong type is coerced to its default rather than raising:
    losing the planner's curated bank because of one bad line is
    strictly worse than losing one bad line.
    """

    out: list[MemoryEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        out.append(
            MemoryEntry(
                source_id=str(obj.get("source_id", "")),
                url=str(obj.get("url", "")),
                title=str(obj.get("title", "")),
                summary=str(obj.get("summary", "")),
                evidence=_coerce_evidence(obj.get("evidence")),
            )
        )
    return out
