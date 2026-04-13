"""Slack message formatting — markdown → mrkdwn conversion + truncation.

Ported from Hermes ``gateway/platforms/slack.py`` (format_message) and
``gateway/platforms/base.py`` (truncate_message).

These are pure functions with no side effects — safe to test in isolation.
"""

from __future__ import annotations

import re
from typing import Optional


def markdown_to_mrkdwn(content: str) -> str:
    """Convert standard markdown to Slack mrkdwn format.

    Protected regions (code blocks, inline code) are extracted first so
    their contents are never modified.  Standard markdown constructs
    (headers, bold, italic, links) are translated to mrkdwn syntax.

    The placeholder system (``\\x00SL{n}\\x00``) ensures transformations
    don't interfere with each other.
    """
    if not content:
        return content

    placeholders: dict = {}
    counter = [0]

    def _ph(value: str) -> str:
        """Stash value behind a placeholder that survives later passes."""
        key = f"\x00SL{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    text = content

    # 1) Protect fenced code blocks (``` ... ```)
    text = re.sub(
        r'(```(?:[^\n]*\n)?[\s\S]*?```)',
        lambda m: _ph(m.group(0)),
        text,
    )

    # 2) Protect inline code (`...`)
    text = re.sub(r'(`[^`]+`)', lambda m: _ph(m.group(0)), text)

    # 3) Convert markdown links [text](url) → <url|text>
    def _convert_markdown_link(m):
        label = m.group(1)
        url = m.group(2).strip()
        if url.startswith('<') and url.endswith('>'):
            url = url[1:-1].strip()
        return _ph(f'<{url}|{label}>')

    text = re.sub(
        r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)',
        _convert_markdown_link,
        text,
    )

    # 4) Protect existing Slack entities/manual links so escaping and later
    #    formatting passes don't break them.
    text = re.sub(
        r'(<(?:[@#!]|(?:https?|mailto|tel):)[^>\n]+>)',
        lambda m: _ph(m.group(1)),
        text,
    )

    # 5) Protect blockquote markers before escaping
    text = re.sub(r'^(>+\s)', lambda m: _ph(m.group(0)), text, flags=re.MULTILINE)

    # 6) Escape Slack control characters in remaining plain text.
    # Unescape first so already-escaped input doesn't get double-escaped.
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # 7) Convert headers (## Title) → *Title* (bold)
    def _convert_header(m):
        inner = m.group(1).strip()
        # Strip redundant bold markers inside a header
        inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
        return _ph(f'*{inner}*')

    text = re.sub(
        r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
    )

    # 8) Convert bold+italic: ***text*** → *_text_* (Slack bold wrapping italic)
    text = re.sub(
        r'\*\*\*(.+?)\*\*\*',
        lambda m: _ph(f'*_{m.group(1)}_*'),
        text,
    )

    # 9) Convert bold: **text** → *text* (Slack bold)
    text = re.sub(
        r'\*\*(.+?)\*\*',
        lambda m: _ph(f'*{m.group(1)}*'),
        text,
    )

    # 10) Convert italic: _text_ stays as _text_ (already Slack italic)
    #     Single *text* → _text_ (Slack italic)
    text = re.sub(
        r'(?<!\*)\*([^*\n]+)\*(?!\*)',
        lambda m: _ph(f'_{m.group(1)}_'),
        text,
    )

    # 11) Convert strikethrough: ~~text~~ → ~text~
    text = re.sub(
        r'~~(.+?)~~',
        lambda m: _ph(f'~{m.group(1)}~'),
        text,
    )

    # 12) Blockquotes: > prefix is already protected by step 5 above.

    # 13) Restore placeholders in reverse order
    for key in reversed(placeholders):
        text = text.replace(key, placeholders[key])

    return text


def truncate_message(content: str, max_length: int = 39000) -> list[str]:
    """Split a long message into chunks, preserving code block boundaries.

    When a split falls inside a triple-backtick code block, the fence is
    closed at the end of the current chunk and reopened (with the original
    language tag) at the start of the next chunk.  Multi-chunk responses
    receive indicators like ``(1/3)``.

    Default *max_length* is 39,000 (Slack's 40K limit minus safety margin).
    """
    if len(content) <= max_length:
        return [content]

    INDICATOR_RESERVE = 10   # room for " (XX/XX)"
    FENCE_CLOSE = "\n```"

    chunks: list[str] = []
    remaining = content
    # When the previous chunk ended mid-code-block, this holds the
    # language tag (possibly "") so we can reopen the fence.
    carry_lang: Optional[str] = None

    while remaining:
        # If we're continuing a code block from the previous chunk,
        # prepend a new opening fence with the same language tag.
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""

        # How much body text we can fit after accounting for the prefix,
        # a potential closing fence, and the chunk indicator.
        headroom = max_length - INDICATOR_RESERVE - len(prefix) - len(FENCE_CLOSE)
        if headroom < 1:
            headroom = max_length // 2

        # Everything remaining fits in one final chunk
        if len(prefix) + len(remaining) <= max_length - INDICATOR_RESERVE:
            chunks.append(prefix + remaining)
            break

        # Find a natural split point (prefer newlines, then spaces)
        region = remaining[:headroom]
        split_at = region.rfind("\n")
        if split_at < headroom // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = headroom

        # Avoid splitting inside an inline code span (`...`).
        # If the text before split_at has an odd number of unescaped
        # backticks, the split falls inside inline code — the resulting
        # chunk would have an unpaired backtick.
        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            # Find the last unescaped backtick and split before it
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                # Try to find a space or newline just before the backtick
                safe_split = candidate.rfind(" ", 0, last_bt)
                nl_split = candidate.rfind("\n", 0, last_bt)
                safe_split = max(safe_split, nl_split)
                if safe_split > headroom // 4:
                    split_at = safe_split

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()

        full_chunk = prefix + chunk_body

        # Walk only the chunk_body (not the prefix we prepended) to
        # determine whether we end inside an open code block.
        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""

        if in_code:
            # Close the orphaned fence so the chunk is valid on its own
            full_chunk += FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None

        chunks.append(full_chunk)

    # Append chunk indicators when the response spans multiple messages
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)
        ]

    return chunks
