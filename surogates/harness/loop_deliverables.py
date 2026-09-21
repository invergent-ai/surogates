"""Catch a turn that ends without the file the user asked for by name.

Two workspace-bench tasks failed this way with the work essentially done.
One produced ``tmp_stage1.docx`` through ``tmp_stage6.docx`` and never
assembled them into the report it was asked for; another read its five
source documents and never wrote anything. Both sessions reported success,
and both scored zero on every rubric.

The check is deliberately narrow. It fires only when the user named a
filename outright -- "write it to report.docx" -- and that exact name
appears nowhere in the turn's tool calls or their results. A name the user
never said is not a deliverable we can hold the model to, and a name that
shows up anywhere in the tool traffic is evidence enough that the file was
handled. Both defaults fail silent, because a wrong nudge costs a turn and
annoys someone whose work was already finished.

No model call: this is string matching over the transcript the loop
already holds.
"""

from __future__ import annotations

import posixpath
import re

#: Extensions worth holding the model to. These are deliverable formats --
#: things a person asks to receive. Source-code extensions are excluded on
#: purpose: "fix the bug in parser.py" names an input, not an output, and
#: the file already exists.
_DELIVERABLE_EXTS = (
    "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt",
    "pdf", "md", "json", "html", "txt",
)

#: Bounded on both sides: the stem repetition is capped so a pathological
#: input cannot drive quadratic backtracking, and the class excludes the
#: separators that would let one match run across two filenames.
_FILENAME_RE = re.compile(
    r"[A-Za-z0-9_\-.]{1,200}\.(?:" + "|".join(_DELIVERABLE_EXTS) + r")\b",
    re.IGNORECASE,
)


def _tool_traffic(messages: list[dict]) -> str:
    """Every tool call argument and result in the turn, as one blob.

    A file can be produced by ``write_file``, by a script the model wrote,
    or by a shell heredoc -- the one thing common to all of them is that
    the name passes through tool arguments. Results are included too, so
    that a directory listing showing the file also counts as evidence.
    """
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
        for call in m.get("tool_calls") or []:
            if isinstance(call, dict):
                args = (call.get("function") or {}).get("arguments")
                if isinstance(args, str):
                    parts.append(args)
    return "\n".join(parts).lower()


def missing_deliverables(
    user_text: str, messages: list[dict], limit: int = 5
) -> list[str]:
    """Filenames the user named that the turn never touched.

    Returns at most *limit* names, in the order the user gave them, so a
    request listing many outputs produces a nudge the model can act on
    rather than a wall of filenames.
    """
    if not user_text:
        return []
    traffic = _tool_traffic(messages)
    missing: list[str] = []
    seen: set[str] = set()
    for match in _FILENAME_RE.finditer(user_text):
        name = posixpath.basename(match.group(0))
        key = name.lower()
        # A bare extension ("...the .docx version") is not a filename.
        if key in seen or key.startswith("."):
            continue
        seen.add(key)
        if key not in traffic:
            missing.append(name)
        if len(missing) >= limit:
            break
    return missing


def deliverable_nudge(names: list[str]) -> str:
    """The nudge text naming what is still missing."""
    listed = ", ".join(names)
    return (
        "[System: The request asked for these files by name and they were "
        f"never written: {listed}. Produce them now, with exactly these "
        "names and extensions, then confirm. If one already exists under a "
        "different name, rename or re-save it to the name above.]"
    )
