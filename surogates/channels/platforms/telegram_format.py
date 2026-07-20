"""Markdown → Telegram-HTML rendering for outbound Telegram messages.

Agents write standard markdown; Telegram's ``sendMessage`` renders either
MarkdownV2 (which requires escaping *every* reserved character and fails the
whole message on one mistake) or a small HTML subset.  HTML is the robust
choice: everything is escaped first, then a conservative markdown subset is
re-introduced as tags Telegram documents (``b``, ``i``, ``code``, ``pre``,
``a``).  :func:`render_plain` is the fallback used when Telegram still
rejects the HTML — same marker stripping, no tags.
"""

from __future__ import annotations

import html
import re

__all__ = ["render_html", "render_plain"]

_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])|(?<![\w_])_([^_\n]+)_(?![\w_])")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)

_PLACEHOLDER = "\x00{}\x00"


def _stash(pattern: re.Pattern, text: str, stash: list[str], wrap) -> str:
    def _sub(match: re.Match) -> str:
        stash.append(wrap(match))
        return _PLACEHOLDER.format(len(stash) - 1)
    return pattern.sub(_sub, text)


def _unstash(text: str, stash: list[str]) -> str:
    for index, chunk in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), chunk)
    return text


def render_html(text: str) -> str:
    """Render markdown *text* as Telegram-safe HTML."""
    stash: list[str] = []
    out = text or ""
    # Code first: its content must never be styled or double-processed.
    out = _stash(
        _FENCE_RE, out, stash,
        lambda m: f"<pre>{html.escape(m.group(1).rstrip())}</pre>",
    )
    out = _stash(
        _INLINE_CODE_RE, out, stash,
        lambda m: f"<code>{html.escape(m.group(1))}</code>",
    )
    out = _stash(
        _LINK_RE, out, stash,
        lambda m: (
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{html.escape(m.group(1), quote=False)}</a>"
        ),
    )
    out = html.escape(out, quote=False)
    out = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", out)
    out = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    out = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", out)
    return _unstash(out, stash)


def render_plain(text: str) -> str:
    """Strip the same markdown subset without introducing any tags.

    Used as the retry body when Telegram rejects the HTML rendering —
    the visitor still gets readable text instead of raw ``**`` noise.
    """
    out = text or ""
    out = _FENCE_RE.sub(lambda m: m.group(1).rstrip(), out)
    out = _INLINE_CODE_RE.sub(lambda m: m.group(1), out)
    out = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", out)
    out = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), out)
    out = _ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), out)
    out = _HEADING_RE.sub(lambda m: m.group(1), out)
    out = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", out)
    return out
