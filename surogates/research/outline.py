"""Pure logic for the living research outline.

The outline is a markdown document the planner rewrites as the
research direction evolves.  These helpers keep it tidy and let
callers enumerate its sections (the writer drives section-by-section
synthesis from the returned list).

Heading parsing is intentionally heading-line-only — no fenced-code
state machine, no inline-code carve-outs.  The planner is instructed
to use markdown ``##``/``###`` headings to mark sections; anything
else inside a fence is the planner's problem to clean up.
"""

from __future__ import annotations

import re

__all__ = ["normalize_outline", "outline_sections"]


# Matches a markdown heading line of level 2..6 and captures the
# whitespace-trimmed title.  Level-1 ``#`` is the report title and is
# NOT a section (the writer emits one section per ``##`` heading).
_HEADING_RE = re.compile(r"^#{2,6}\s+(.*\S)\s*$")


def normalize_outline(text: str) -> str:
    """Strip trailing whitespace per line and collapse blank-line runs.

    Also strips leading and trailing blank lines so the outline does
    not grow unbounded across re-saves.  Internal whitespace inside a
    line is preserved verbatim — only line-end and blank-run noise
    is removed.
    """

    lines = [line.rstrip() for line in text.splitlines()]

    collapsed: list[str] = []
    pending_blank = False
    for line in lines:
        if line == "":
            # Emit one blank only when something non-blank has already
            # been written; this drops leading blanks and merges runs.
            if collapsed:
                pending_blank = True
            continue
        if pending_blank:
            collapsed.append("")
            pending_blank = False
        collapsed.append(line)

    return "\n".join(collapsed)


def outline_sections(text: str) -> list[str]:
    """Return heading titles (level 2 and below) in document order.

    Level-1 ``#`` headings are excluded — they are the report title.
    The returned list is what the writer iterates over to call
    ``research_memory(action="retrieve", query=<section>)`` once per
    section.
    """

    sections: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            sections.append(match.group(1).strip())
    return sections
