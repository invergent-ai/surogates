"""Bounded, verbatim context around a verified source passage.

The caller authorizes and verifies the artifact before using these helpers.
Offsets always refer to the original text, locally within a PDF page. No
headings, table cells, page numbers or continuation relationships are inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

CONTEXT_LIMIT_DEFAULT = 8_000
CONTEXT_LIMIT_MAX = 24_000


@dataclass(frozen=True)
class EvidenceSpan:
    page_number: int | None
    start: int
    end: int
    total: int
    content: str


def _around(text: str, start: int, end: int, budget: int) -> tuple[int, int]:
    """Grow both sides of an anchor, spending spare space at document edges."""
    size = min(len(text), budget)
    left = max(0, start - (size - (end - start)) // 2)
    left = min(left, len(text) - size)
    return left, left + size


def surrounding_spans(
    raw: bytes,
    *,
    page_number: int | None,
    start: int,
    end: int,
    budget: int = CONTEXT_LIMIT_DEFAULT,
) -> list[EvidenceSpan]:
    """Keep the anchor; expand its page, then the preceding tail/following head.

    PDF neighbors must have consecutive page numbers. The matched page takes
    priority; remaining space is split evenly between neighbors, redistributing
    unused space. Markdown gets one continuous window around the anchor.
    The sum of returned source characters never exceeds the clamped budget.
    """
    budget = min(budget, CONTEXT_LIMIT_MAX)
    if budget <= 0:
        raise ValueError("context limit must be positive")
    text = raw.decode("utf-8", errors="replace")
    bodies: dict[int | None, str] = {None: text}
    if page_number is not None:
        try:
            pages = json.loads(text)
        except ValueError as exc:
            raise ValueError("published artifact is not a PDF page array") from exc
        if not isinstance(pages, list):
            raise ValueError("published artifact is not a PDF page array")
        bodies = {}
        for page in pages:
            if (
                not isinstance(page, dict)
                or type(page.get("page")) is not int
                or page["page"] < 1
                or page["page"] in bodies
                or not isinstance(page.get("content") or "", str)
            ):
                raise ValueError("published PDF pages are malformed or duplicated")
            bodies[page["page"]] = page.get("content") or ""
        if page_number not in bodies:
            raise ValueError("indexed PDF page is missing from its published artifact")
        text = bodies[page_number]
    if not 0 <= start < end <= len(text):
        raise ValueError("indexed passage offsets do not match the published artifact")
    if budget < end - start:
        raise ValueError(
            f"context limit must be at least {end - start} to retain the passage"
        )

    left, right = _around(text, start, end, budget)
    spans = [EvidenceSpan(page_number, left, right, len(text), text[left:right])]
    remaining = budget - (right - left)
    if page_number is None or not remaining:
        return spans

    previous = bodies.get(page_number - 1, "")
    following = bodies.get(page_number + 1, "")
    before = min(len(previous), remaining // 2)
    after = min(len(following), remaining - before)
    before = min(len(previous), remaining - after)
    if before:
        spans.insert(
            0,
            EvidenceSpan(
                page_number - 1,
                len(previous) - before,
                len(previous),
                len(previous),
                previous[-before:],
            ),
        )
    if after:
        spans.append(
            EvidenceSpan(
                page_number + 1,
                0,
                after,
                len(following),
                following[:after],
            )
        )
    return spans


def render_surrounding(spans: list[EvidenceSpan]) -> str:
    """Separate source slices explicitly; never present omitted text as contiguous."""
    blocks = []
    for span in spans:
        location = (
            "Document" if span.page_number is None else f"Page {span.page_number}"
        )
        blocks.append(
            f"## {location} — characters {span.start}-{span.end} of {span.total}\n\n"
            + ("[Earlier text omitted]\n\n" if span.start else "")
            + span.content
            + ("\n\n[Later text omitted]" if span.end < span.total else "")
        )
    if spans[0].page_number is None:
        continuation = (
            "For more context, omit passage_id and context; use offset with the "
            "document character positions above."
        )
    else:
        continuation = (
            "For more context, omit passage_id and context; use pages to select "
            "a PDF range, then offset to continue that range."
        )
    return "\n\n".join(blocks) + "\n\n_" + continuation + "_"
