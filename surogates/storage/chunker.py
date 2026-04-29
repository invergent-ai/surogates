"""Markdown chunker for the KB retrieval layer.

Splits markdown text into chunks the wiki-maintainer + retrieval path
operate on. Each chunk carries a ``heading_path`` breadcrumb (e.g.
``"Sub-Agents > What is a Sub-Agent?"``) so search results can surface
the section context without re-reading the parent doc.

The chunker is deterministic and stdlib-only — no tiktoken, no NLP
models. Sizing is in characters not tokens; the conversion is roughly
``chars / 4 ≈ tokens`` for English text. Defaults target ~500 tokens
per chunk with ~50 tokens of overlap on oversized sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_MAX_CHARS = 2000
DEFAULT_OVERLAP = 200

# ATX-style headings: 1-6 ``#`` characters followed by space + title.
# We deliberately skip the alternate underline style ('===' / '---')
# because GitHub-flavored docs almost universally use ATX.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieval-unit chunk produced by :func:`chunk_markdown`."""

    content: str
    heading_path: Optional[str]
    chunk_index: int


def chunk_markdown(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split *text* into chunks, never larger than *max_chars* characters.

    Sections (text under a heading, until the next equal-or-higher
    heading) that fit go out as one chunk. Sections that don't fit get
    sliding-window-split with *overlap* characters carried over so a
    sentence is unlikely to be cut mid-thought across the boundary.

    Returns an empty list for empty input. ``heading_path`` is ``None``
    only for content that precedes the first heading in the document.
    """
    if not text or not text.strip():
        return []

    sections = _split_by_headings(text)
    chunks: list[Chunk] = []
    idx = 0

    for path, body in sections:
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chars:
            chunks.append(Chunk(content=body, heading_path=path, chunk_index=idx))
            idx += 1
            continue

        # Sliding window over a long section.
        start = 0
        while start < len(body):
            end = min(start + max_chars, len(body))
            chunks.append(
                Chunk(
                    content=body[start:end],
                    heading_path=path,
                    chunk_index=idx,
                )
            )
            idx += 1
            if end >= len(body):
                break
            # Step forward by max_chars - overlap so the next chunk
            # starts inside the previous one.
            start = max(end - overlap, start + 1)

    return chunks


def _split_by_headings(text: str) -> list[tuple[Optional[str], str]]:
    """Return list of ``(heading_path, body)`` for each section.

    The heading_path stacks ancestors: an h1 'Foo' followed by an h2
    'Bar' yields a 'Foo > Bar' breadcrumb for content under Bar. A
    later h1 'Baz' resets the stack.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]

    parts: list[tuple[Optional[str], str]] = []

    # Pre-text: whatever sits before the first heading gets ``None`` path.
    pre = text[: matches[0].start()].strip()
    if pre:
        parts.append((None, pre))

    stack: list[tuple[int, str]] = []  # (level, title) for current ancestors
    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        # Pop equal-or-higher levels off the stack so 'h2' under 'h1'
        # stays nested but a later 'h1' resets to top-level.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack)

        body_start = m.end()
        body_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(text)
        )
        body = text[body_start:body_end].strip()
        # Even an empty body still records the heading so we know it
        # existed, but emit only if there's content (so chunk_markdown
        # doesn't produce a zero-content chunk).
        parts.append((path, body))

    return parts
