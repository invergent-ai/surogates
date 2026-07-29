"""Markdown → WhatsApp markup transcoder.

WhatsApp renders its own lightweight markup, not markdown and not HTML:
``*bold*``, ``_italic_``, ``~strike~`` and triple-backtick monospace.  The
platform prompt tells the model to emit no markdown, but models routinely
do, so every outbound body is transcoded before it is split and sent.

Code spans are protected by replacing them with ``\\x00CODE{i}\\x00``
sentinels before any transformation and restoring them afterwards.  The
**trailing** ``\\x00`` is load-bearing: without it the sequential
``str.replace`` restore of index 1 would corrupt index 11.  ``\\x00`` never
appears in LLM output.
"""

from __future__ import annotations

import re

__all__ = ["render_whatsapp"]

# Fenced blocks first (greedy over newlines), then inline spans.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# ``***x***`` must run before ``**x**`` or the outer pair is consumed first.
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
# Runs last, once every ``**`` pair has become a ``_BOLD`` token, so any
# surviving ``*`` is genuine markdown italic.  Every guard here is
# load-bearing; an unguarded ``\*(.+?)\*`` mangles ordinary prose:
#
#   ``* one\n* two``               -> ``_ one\n_ two``   (bullet list)
#   ``5 * 3 = 15 and 2 * 4 = 8``   -> ``5 _ 3 = 15 …``   (arithmetic)
#
# The word-boundary lookarounds and newline-free body come from
# ``telegram_format``.  The whitespace guards are additional: they encode
# markdown's flanking rule (a delimiter may not be followed — or preceded —
# by whitespace), which is what separates ``*italic*`` from ``2 * 4``.
# ``telegram_format`` omits them and mis-renders the arithmetic case.
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]*?)(?<![\s*])\*(?![\w*])")

_SENTINEL = "\x00"

# Emphasis is emitted as control-character tokens rather than the final
# ``*``/``_``/``~`` markers, then substituted in one pass at the end.
# Writing WhatsApp's markers directly would feed them back to the later
# rules: ``**bold**`` → ``*bold*`` would then be re-read as italic, and
# ``***both***`` → ``*_both_*`` as ``__both__``.  Like ``\x00`` above,
# these never appear in LLM output.
_BOLD = "\x01"
_ITALIC = "\x02"
_STRIKE = "\x03"


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace code spans with sentinels; return ``(masked_text, spans)``."""
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"{_SENTINEL}CODE{len(spans) - 1}{_SENTINEL}"

    text = _FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    return text, spans


def _restore(text: str, spans: list[str]) -> str:
    """Put the protected code spans back."""
    for index, span in enumerate(spans):
        text = text.replace(f"{_SENTINEL}CODE{index}{_SENTINEL}", span)
    return text


def render_whatsapp(text: str) -> str:
    """Convert *text* from markdown to WhatsApp markup.

    Nothing is escaped: WhatsApp has no escape syntax, so a literal
    asterisk in the source is not representable.  Lists, blockquotes and
    tables pass through untouched — WhatsApp renders none of them and the
    raw characters read acceptably.
    """
    if not text:
        return ""

    text, spans = _protect(text)

    text = _BOLD_ITALIC_RE.sub(f"{_BOLD}{_ITALIC}\\1{_ITALIC}{_BOLD}", text)
    text = _BOLD_RE.sub(f"{_BOLD}\\1{_BOLD}", text)
    text = _BOLD_UNDERSCORE_RE.sub(f"{_BOLD}\\1{_BOLD}", text)
    text = _STRIKE_RE.sub(f"{_STRIKE}\\1{_STRIKE}", text)
    text = _HEADER_RE.sub(f"{_BOLD}\\1{_BOLD}", text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _ITALIC_RE.sub(f"{_ITALIC}\\1{_ITALIC}", text)

    text = text.replace(_BOLD, "*").replace(_ITALIC, "_").replace(_STRIKE, "~")

    return _restore(text, spans)
