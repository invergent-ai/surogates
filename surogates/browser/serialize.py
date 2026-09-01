"""Markdown rendering of a browser page snapshot.

The page tree from ``KernelBrowserClient.get_state`` is a flat, document-ordered
node list.  This module renders it as markdown: interactive nodes become
addressable ``@eN`` lines, text blocks become plain lines, and heading roles
provide the structure.  Pure -- no I/O, no client, no page access -- so it is
testable in isolation and cannot fail on a slow page.
"""

from __future__ import annotations

from typing import Any

# Interactive roles get a ``- role @eN "name"`` line whether or not they own
# text.  Mirrors ``KernelBrowserClient._INTERACTIVE_ROLES``; kept as a separate
# constant so the serializer does not import the HTTP client.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "menuitem",
    "tab",
    "switch",
    "searchbox",
    "slider",
    "spinbutton",
    "option",
    "file-input",
})

# Cap on emitted nodes.  Deliberately not a tool parameter: ``browser_evaluate``
# is the escape hatch for reading past it, and extracting 1200 rows in one call
# beats re-requesting a bigger tree.
MAX_MARKDOWN_NODES: int = 500

_MIN_HEADING_LEVEL: int = 2
_MAX_HEADING_LEVEL: int = 6


def render_markdown(state: dict[str, Any]) -> str:
    """Render a ``get_state`` result as markdown."""

    viewport = state.get("viewport") or {}
    lines: list[str] = [
        f"# {state.get('title', '')}",
        str(state.get("url", "")),
        f"viewport {int(viewport.get('width', 0))}x{int(viewport.get('height', 0))}",
        "",
    ]

    tree = state.get("tree") or []
    emitted = 0
    capped = False
    for entry in tree:
        if emitted >= MAX_MARKDOWN_NODES:
            capped = True
            break
        line = _render_entry(entry)
        if line is None:
            continue
        lines.append(line)
        emitted += 1

    if not emitted:
        lines.append("(no visible elements)")
    elif capped:
        # Gated on ``capped`` rather than ``len(tree) > emitted``: nodes that
        # render silently (spans covered by an ancestor text block, containers
        # owning no text) make ``emitted`` fall short of ``len(tree)`` on
        # virtually every real page, and a length comparison would append a
        # truncation notice when nothing was actually cut.
        lines.append("")
        lines.append(
            f"[truncated: {emitted} of {len(tree)} nodes shown — narrow with "
            f"selector, or read the rest with browser_evaluate]"
        )

    return "\n".join(lines)


def _render_entry(entry: dict[str, Any]) -> str | None:
    """Return the markdown line for one node, or ``None`` to skip it."""

    role = str(entry.get("role", ""))
    if role == "heading":
        text = str(entry.get("text_block") or "").strip()
        if not text:
            return None
        return f"{'#' * _heading_level(entry)} {text}"

    if role in INTERACTIVE_ROLES:
        return f'- {role} {entry.get("ref", "")} "{entry.get("name", "")}"'

    text = str(entry.get("text_block") or "").strip()
    return text or None


def _heading_level(entry: dict[str, Any]) -> int:
    try:
        level = int(entry.get("heading_level", _MIN_HEADING_LEVEL))
    except (TypeError, ValueError):
        level = _MIN_HEADING_LEVEL
    return max(_MIN_HEADING_LEVEL, min(_MAX_HEADING_LEVEL, level))
