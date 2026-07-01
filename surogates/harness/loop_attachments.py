"""Attachment note and inline-rendering helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.harness.loop_messages import _format_bytes
from surogates.session.events import EventType

_ATTACHMENT_SKIP_HINTS: dict[str, str] = {
    "parse_error": (
        "try read_file with pdftotext/pandoc fallbacks"
    ),
    "parse_timeout": (
        "the parser hit its wall-clock cap; try read_file with a narrower offset/limit"
    ),
    "decode_error": (
        "the file is not UTF-8; try read_file which has full BOM detection"
    ),
    "oversize_output": (
        "the parsed content exceeded the inline cap; use read_file with"
        " offset/limit to paginate"
    ),
    "empty_output": (
        "the parser produced no text; the file may be a scan — try the"
        " ocr-and-documents skill"
    ),
    "total_budget_exceeded": (
        "earlier attachments already filled the inline-budget; use read_file"
        " when you actually need this file's content"
    ),
}
def _attachments_note(events: list[Any]) -> str | None:
    """Return a per-turn system note describing path-only attachments.

    Reads ``data.attachments`` on the most recent ``user.message``
    event.  Any attachment whose ``inlined_text`` is already populated
    is omitted from this note (the content lives in the user message
    text via :func:`_render_inlined_attachments`).  Attachments that
    were candidates for inline but skipped get an annotated entry that
    names the ``inline_skip_reason`` so the agent knows why it needs to
    fall back to ``read_file``.

    The lookup is read-only and never raises — malformed payloads
    (e.g. ``attachments`` not a list, items not dicts) yield ``None``
    so the LLM call proceeds unchanged.
    """
    for event in reversed(events):
        event_type = event.type
        type_value = (
            event_type.value if hasattr(event_type, "value") else event_type
        )
        if type_value != EventType.USER_MESSAGE.value:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        return _attachments_note_from_data(data)
    return None


def _attachments_note_from_data(data: Any) -> str | None:
    """Pure helper: render the attachments note from a user.message data dict."""
    if not isinstance(data, dict):
        return None

    # Section 1: downloaded/workspace attachments (existing behaviour preserved).
    attachments_section: str | None = None
    attachments = data.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines = [
            "The user attached the following files to this message. They are"
            " available in the workspace and you can read them with your file"
            " tools:",
        ]
        for item in attachments:
            if not isinstance(item, dict):
                continue
            if item.get("inlined_text"):
                # Content already in the user message text via
                # _render_inlined_attachments — don't double-list it here.
                continue
            path = item.get("path")
            filename = item.get("filename")
            if not path or not filename:
                continue
            mime = item.get("mime_type") or "application/octet-stream"
            raw_size = item.get("size")
            if isinstance(raw_size, (int, float)) and raw_size >= 0:
                size_str = _format_bytes(int(raw_size))
            else:
                size_str = "unknown size"
            line = f"- {path} ({mime}, {size_str}) — \"{filename}\""
            skip_reason = item.get("inline_skip_reason")
            if skip_reason:
                hint = _ATTACHMENT_SKIP_HINTS.get(skip_reason, "use read_file")
                line += f" (inline skipped: {skip_reason} — {hint})"
            lines.append(line)

        if len(lines) > 1:
            attachments_section = "\n".join(lines)

    # Section 2: channel file ids from source.files (additive, never raises).
    files_section: str | None = None
    try:
        source_files = (data.get("source") or {}).get("files")
        if isinstance(source_files, list):
            file_lines = []
            for f in source_files:
                if not isinstance(f, dict):
                    continue
                fid = f.get("id")
                if not fid:
                    continue
                name = f.get("name") or "file"
                file_lines.append(f"- \"{name}\" — file id {fid}")
            if file_lines:
                header = (
                    "These files were shared in this channel. If one is not"
                    " already in your workspace above, download it on demand"
                    " with fetch_channel_file(\"<id>\"):"
                )
                files_section = header + "\n" + "\n".join(file_lines)
    except Exception:
        pass

    if attachments_section and files_section:
        return attachments_section + "\n\n" + files_section
    if attachments_section:
        return attachments_section
    if files_section:
        return files_section
    return None


def _render_inlined_attachments(
    content: str,
    attachments: list[Any] | None,
) -> str:
    """Append one fenced block per inlined attachment to ``content``.

    ``attachments`` is the persisted ``data["attachments"]`` payload
    from a ``user.message`` event.  Each item with a non-empty
    ``inlined_text`` field becomes a fenced block at the end of the
    returned string.  Items without ``inlined_text`` (path-only,
    inline-skipped, or unsupported) are ignored here -- the system
    ``_attachments_note`` surface covers them.
    """
    if not attachments:
        return content
    blocks: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        inlined = item.get("inlined_text")
        if not inlined:
            continue
        kind = item.get("inlined_render_kind") or "text"
        path = item.get("path") or ""
        filename = item.get("filename") or path
        header = f"**Attachment: {filename}**"
        if kind == "markdown":
            subtitle = (
                "*(parsed via liteparse — to re-read or "
                f"paginate, use `read_file(\"{path}\")`)*"
            )
            block = f"---\n{header}\n{subtitle}\n\n{inlined}\n---"
        else:
            block = f"---\n{header}\n\n{inlined}\n---"
        blocks.append(block)
    if not blocks:
        return content
    return content + "\n\n" + "\n\n".join(blocks)
