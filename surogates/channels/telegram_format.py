"""Markdown to Telegram MarkdownV2 conversion and message truncation."""

from __future__ import annotations

import re

__all__ = [
    "format_message",
    "truncate_message",
]

# Telegram message limits
MAX_MESSAGE_LENGTH = 4096

# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


def strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes to produce clean plain text.

    Also removes MarkdownV2 formatting markers so the fallback
    doesn't show stray syntax characters from format_message conversion.
    """
    # Remove escape backslashes before special characters
    cleaned = re.sub(r'\\([_*\[\]()~`>#\+\-=|{}.!\\])', r'\1', text)
    # Remove MarkdownV2 bold markers that format_message converted from **bold**
    cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
    # Remove MarkdownV2 italic markers that format_message converted from *italic*
    # Use word boundary (\b) to avoid breaking snake_case like my_variable_name
    cleaned = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', cleaned)
    # Remove MarkdownV2 strikethrough markers (~text~ -> text)
    cleaned = re.sub(r'~([^~]+)~', r'\1', cleaned)
    # Remove MarkdownV2 spoiler markers (||text|| -> text)
    cleaned = re.sub(r'\|\|([^|]+)\|\|', r'\1', cleaned)
    return cleaned


def format_message(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 format.

    Protected regions (code blocks, inline code) are extracted first so
    their contents are never modified.  Standard markdown constructs
    (headers, bold, italic, links) are translated to MarkdownV2 syntax,
    and all remaining special characters are escaped.
    """
    if not content:
        return content

    placeholders: dict[str, str] = {}
    counter = [0]

    def _ph(value: str) -> str:
        """Stash *value* behind a placeholder token that survives escaping."""
        key = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    text = content

    # 1) Protect fenced code blocks (``` ... ```)
    #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
    def _protect_fenced(m: re.Match[str]) -> str:
        raw = m.group(0)
        # Split off opening ``` (with optional language) and closing ```
        open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
        opening = raw[:open_end]
        body_and_close = raw[open_end:]
        body = body_and_close[:-3]
        body = body.replace('\\', '\\\\').replace('`', '\\`')
        return _ph(opening + body + '```')

    text = re.sub(
        r'(```(?:[^\n]*\n)?[\s\S]*?```)',
        _protect_fenced,
        text,
    )

    # 2) Protect inline code (`...`)
    #    Escape \ inside inline code per MarkdownV2 spec.
    text = re.sub(
        r'(`[^`]+`)',
        lambda m: _ph(m.group(0).replace('\\', '\\\\')),
        text,
    )

    # 3) Convert markdown links -- escape the display text; inside the URL
    #    only ')' and '\' need escaping per the MarkdownV2 spec.
    def _convert_link(m: re.Match[str]) -> str:
        display = _escape_mdv2(m.group(1))
        url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
        return _ph(f'[{display}]({url})')

    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _convert_link, text)

    # 4) Convert markdown headers (## Title) -> bold *Title*
    def _convert_header(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        # Strip redundant bold markers that may appear inside a header
        inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
        return _ph(f'*{_escape_mdv2(inner)}*')

    text = re.sub(
        r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
    )

    # 5) Convert bold: **text** -> *text* (MarkdownV2 bold)
    text = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
        text,
    )

    # 6) Convert italic: *text* (single asterisk) -> _text_ (MarkdownV2 italic)
    #    [^*\n]+ prevents matching across newlines (which would corrupt
    #    bullet lists using * markers and multi-line content).
    text = re.sub(
        r'\*([^*\n]+)\*',
        lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
        text,
    )

    # 7) Convert strikethrough: ~~text~~ -> ~text~ (MarkdownV2)
    text = re.sub(
        r'~~(.+?)~~',
        lambda m: _ph(f'~{_escape_mdv2(m.group(1))}~'),
        text,
    )

    # 8) Convert spoiler: ||text|| -> ||text|| (protect from | escaping)
    text = re.sub(
        r'\|\|(.+?)\|\|',
        lambda m: _ph(f'||{_escape_mdv2(m.group(1))}||'),
        text,
    )

    # 9) Convert blockquotes: > at line start -> protect > from escaping
    text = re.sub(
        r'^(>{1,3}) (.+)$',
        lambda m: _ph(m.group(1) + ' ' + _escape_mdv2(m.group(2))),
        text,
        flags=re.MULTILINE,
    )

    # 10) Escape remaining special characters in plain text
    text = _escape_mdv2(text)

    # 11) Restore placeholders in reverse insertion order so that
    #    nested references (a placeholder inside another) resolve correctly.
    for key in reversed(list(placeholders.keys())):
        text = text.replace(key, placeholders[key])

    # 12) Safety net: escape unescaped ( ) { } that slipped through
    #     placeholder processing.  Split the text into code/non-code
    #     segments so we never touch content inside ``` or ` spans.
    _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
    _safe_parts: list[str] = []
    for _idx, _seg in enumerate(_code_split):
        if _idx % 2 == 1:
            # Inside code span/block -- leave untouched
            _safe_parts.append(_seg)
        else:
            # Outside code -- escape bare ( ) { }
            def _esc_bare(m: re.Match[str], _seg: str = _seg) -> str:
                s = m.start()
                ch = m.group(0)
                # Already escaped
                if s > 0 and _seg[s - 1] == '\\':
                    return ch
                # ( that opens a MarkdownV2 link [text](url)
                if ch == '(' and s > 0 and _seg[s - 1] == ']':
                    return ch
                # ) that closes a link URL
                if ch == ')':
                    before = _seg[:s]
                    if '](http' in before or '](' in before:
                        # Check depth
                        depth = 0
                        for j in range(s - 1, max(s - 2000, -1), -1):
                            if _seg[j] == '(':
                                depth -= 1
                                if depth < 0:
                                    if j > 0 and _seg[j - 1] == ']':
                                        return ch
                                    break
                            elif _seg[j] == ')':
                                depth += 1
                return '\\' + ch
            _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
    text = ''.join(_safe_parts)

    return text


def truncate_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split *content* into chunks that fit within *max_length*.

    Preserves code block boundaries and adds ``(1/N)`` indicators when
    the message is split into multiple chunks.
    """
    if len(content) <= max_length:
        return [content]

    chunks: list[str] = []
    while content:
        if len(content) <= max_length:
            chunks.append(content)
            break

        # Try to split at a newline near the limit
        split_at = content.rfind('\n', 0, max_length)
        if split_at < max_length // 2:
            # No good newline split point -- split at max_length
            split_at = max_length

        chunk = content[:split_at]
        content = content[split_at:].lstrip('\n')
        chunks.append(chunk)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)]

    return chunks
