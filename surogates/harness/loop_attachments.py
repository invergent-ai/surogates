"""Attachment note and inline-rendering helpers for the harness loop."""

from __future__ import annotations

from typing import Any

from surogates.harness.loop_messages import (
    _format_bytes,
    _view_context_note_from_metadata,
)
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
    attachments = data.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return None

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

    if len(lines) == 1:
        # All items malformed or all inlined.
        return None
    return "\n".join(lines)


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


def fold_attachment_context(text: str, data: Any) -> str:
    """Fold a ``user.message`` event's attachment context into ``text``.

    Mirrors what :meth:`AgentHarness._rebuild_messages` does for a normal
    user turn: render inlined attachment blocks onto ``text`` and prepend
    the view-context and attachments notes.  Used to preserve attachment
    context when the rebuilt user content is replaced wholesale (e.g.
    slash-skill expansion overwriting it with just the skill body), which
    would otherwise drop the note and the inlined file content so the
    agent acts as if no file was attached.

    ``data`` is the ``user.message`` event's ``data`` dict.  Returns
    ``text`` unchanged when there is no attachment/view context to fold.
    """
    if not isinstance(data, dict):
        return text
    out = _render_inlined_attachments(text, data.get("attachments"))
    note_parts: list[str] = []
    view_note = _view_context_note_from_metadata(data.get("metadata"))
    if view_note:
        note_parts.append(view_note)
    attachments_note = _attachments_note_from_data(data)
    if attachments_note:
        note_parts.append(attachments_note)
    if note_parts:
        notes_block = "\n\n".join(note_parts)
        out = f"{notes_block}\n\n{out}" if out else notes_block
    return out


def build_user_message_dict(
    event_data: Any, base_content: str | None = None,
) -> dict:
    """Build the LLM user-message dict for a ``user.message`` event.

    Single source of truth for per-user-message content construction:
    folds the attachment note + inlined file content + view-context note
    into the text (via :func:`fold_attachment_context`) and, when the
    event carries images, assembles a multimodal ``content`` blocks list
    (text + ``image_url`` parts) and shrinks oversized images.

    ``base_content`` overrides the event's raw ``content`` as the text
    base.  The slash-skill and ``/deep-research`` rewrite paths pass the
    rewritten body here so the *current turn's* attachment note, inlined
    file content, and image blocks survive the rewrite instead of being
    clobbered.  Returns ``{"role": "user", "content": str | list}``.
    """
    data = event_data if isinstance(event_data, dict) else {}
    base = data.get("content", "") if base_content is None else base_content
    text = fold_attachment_context(base, data)

    images = data.get("images")
    if not images:
        return {"role": "user", "content": text}

    blocks: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        data_url = img["data"]
        if not data_url.startswith("data:"):
            mime = img.get("mime_type", "image/png")
            data_url = f"data:{mime};base64,{data_url}"
        blocks.append({
            "type": "image_url",
            "image_url": {"url": data_url, "detail": "auto"},
        })
    msg = {"role": "user", "content": blocks}
    from surogates.harness.image_shrink import shrink_image_parts_in_messages
    shrink_image_parts_in_messages([msg])
    return msg
