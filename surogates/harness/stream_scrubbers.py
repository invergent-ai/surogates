"""Stateful scrubbers for provider text streamed across delta boundaries."""

from __future__ import annotations

from typing import ClassVar, Final

# ---------------------------------------------------------------------------
# Upstream-gateway error sentinels
# ---------------------------------------------------------------------------
#
# Some third-party OpenAI-compatible gateways (e.g. yunwu.ai) do not surface an
# error status when their own upstream returns nothing.  Instead they fabricate
# a normal-looking chat completion whose ``content`` is a hardcoded error string
# -- often localized -- and attach ``finish_reason='tool_calls'`` with a bogus
# tool call.  Because it arrives as ordinary streamed content, it bypasses every
# empty-response guard and is shown verbatim to the end user.
#
# These are the exact strings such gateways emit.  ``StreamingSentinelScrubber``
# suppresses them mid-stream; ``is_upstream_error_sentinel`` recognises a fully
# assembled message.  Both are content filters only -- they never alter a
# genuine model response (see the "must stay green" tests).
UPSTREAM_ERROR_SENTINELS: Final[tuple[str, ...]] = (
    # yunwu.ai — "Upstream model returned no content. Possible causes: safety
    # policy triggered, upstream rate-limited, or the model ended on this
    # input. Retry or simplify the input."
    "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
    "或模型对当前输入直接结束。请重试或简化输入后再试。",
)

# Distinctive fragments that uniquely identify a gateway error even if the
# surrounding wording drifts.  Kept long and gateway-specific so they can never
# collide with legitimate assistant text.
_UPSTREAM_SENTINEL_MARKERS: Final[tuple[str, ...]] = (
    "上游模型未返回任何内容",
)


def _contains_cjk(text: str) -> bool:
    """True if *text* contains a CJK ideograph.

    The gateway error strings are Chinese; a lone shared lead-in such as the
    ``⚠️`` warning emoji contains none.  Used on flush to distinguish a
    truncated gateway error (suppress) from an innocuous prefix (emit).
    """
    return any(
        "㐀" <= ch <= "鿿"  # CJK Unified Ideographs (incl. Ext-A)
        or "豈" <= ch <= "﫿"  # CJK Compatibility Ideographs
        for ch in text
    )


def is_upstream_error_sentinel(text: str | None) -> bool:
    """Return ``True`` if *text* is a known upstream-gateway error string.

    Matches a message that *is* (starts with) a full sentinel, or that contains
    a distinctive gateway marker.  Used to filter fully assembled messages on
    both the streaming and non-streaming paths.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if any(stripped.startswith(sentinel) for sentinel in UPSTREAM_ERROR_SENTINELS):
        return True
    return any(marker in stripped for marker in _UPSTREAM_SENTINEL_MARKERS)


class StreamingThinkScrubber:
    """Remove streamed thinking/reasoning XML blocks without leaking split tags."""

    _OPEN_TAG_NAMES: ClassVar[tuple[str, ...]] = (
        "think",
        "thinking",
        "reasoning",
        "thought",
        "REASONING_SCRATCHPAD",
    )
    _OPEN_TAGS: ClassVar[tuple[str, ...]] = tuple(
        f"<{name}>" for name in _OPEN_TAG_NAMES
    )
    _CLOSE_TAGS: ClassVar[tuple[str, ...]] = tuple(
        f"</{name}>" for name in _OPEN_TAG_NAMES
    )
    _MAX_TAG_LEN: ClassVar[int] = max(len(tag) for tag in _OPEN_TAGS + _CLOSE_TAGS)

    def __init__(self) -> None:
        self._in_block = False
        self._buf = ""
        self._last_emitted_ended_newline = True

    def reset(self) -> None:
        self._in_block = False
        self._buf = ""
        self._last_emitted_ended_newline = True

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_block:
                close_idx, close_len = self._find_first_tag(buf, self._CLOSE_TAGS)
                if close_idx == -1:
                    held = self._max_partial_suffix(buf, self._CLOSE_TAGS)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[close_idx + close_len:]
                self._in_block = False
                continue

            pair = self._find_earliest_closed_pair(buf)
            open_idx, open_len = self._find_open_at_boundary(buf, out)

            if pair is not None and (open_idx == -1 or pair[0] <= open_idx):
                start_idx, end_idx = pair
                self._append_visible(out, buf[:start_idx])
                buf = buf[end_idx:]
                continue

            if open_idx != -1:
                self._append_visible(out, buf[:open_idx])
                self._in_block = True
                buf = buf[open_idx + open_len:]
                continue

            held = max(
                self._max_partial_suffix(buf, self._OPEN_TAGS),
                self._max_partial_suffix(buf, self._CLOSE_TAGS),
            )
            emit_text = buf[:-held] if held else buf
            self._buf = buf[-held:] if held else ""
            self._append_visible(out, emit_text)
            return "".join(out)

        return "".join(out)

    def flush(self) -> str:
        if self._in_block:
            self._buf = ""
            self._in_block = False
            return ""
        tail = self._strip_orphan_close_tags(self._buf)
        self._buf = ""
        if tail:
            self._last_emitted_ended_newline = tail.endswith("\n")
        return tail

    def _append_visible(self, out: list[str], text: str) -> None:
        if not text:
            return
        text = self._strip_orphan_close_tags(text)
        if not text:
            return
        out.append(text)
        self._last_emitted_ended_newline = text.endswith("\n")

    @staticmethod
    def _find_first_tag(buf: str, tags: tuple[str, ...]) -> tuple[int, int]:
        lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in tags:
            idx = lower.find(tag.lower())
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_len = len(tag)
        return best_idx, best_len

    def _find_earliest_closed_pair(self, buf: str) -> tuple[int, int] | None:
        lower = buf.lower()
        best: tuple[int, int] | None = None
        for open_tag, close_tag in zip(self._OPEN_TAGS, self._CLOSE_TAGS):
            open_idx = lower.find(open_tag.lower())
            if open_idx == -1:
                continue
            close_idx = lower.find(close_tag.lower(), open_idx + len(open_tag))
            if close_idx == -1:
                continue
            pair = (open_idx, close_idx + len(close_tag))
            if best is None or pair[0] < best[0]:
                best = pair
        return best

    def _find_open_at_boundary(
        self,
        buf: str,
        already_emitted: list[str],
    ) -> tuple[int, int]:
        lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in self._OPEN_TAGS:
            start = 0
            tag_lower = tag.lower()
            while True:
                idx = lower.find(tag_lower, start)
                if idx == -1:
                    break
                if self._is_boundary(buf, idx, already_emitted):
                    if best_idx == -1 or idx < best_idx:
                        best_idx = idx
                        best_len = len(tag)
                    break
                start = idx + 1
        return best_idx, best_len

    def _is_boundary(
        self,
        buf: str,
        idx: int,
        already_emitted: list[str],
    ) -> bool:
        if idx == 0:
            if already_emitted:
                return already_emitted[-1].endswith("\n")
            return self._last_emitted_ended_newline
        preceding = buf[:idx]
        last_newline = preceding.rfind("\n")
        if last_newline == -1:
            prior_newline = (
                already_emitted[-1].endswith("\n")
                if already_emitted
                else self._last_emitted_ended_newline
            )
            return prior_newline and preceding.strip() == ""
        return preceding[last_newline + 1:].strip() == ""

    @classmethod
    def _max_partial_suffix(cls, buf: str, tags: tuple[str, ...]) -> int:
        lower = buf.lower()
        max_check = min(len(lower), cls._MAX_TAG_LEN - 1)
        for size in range(max_check, 0, -1):
            suffix = lower[-size:]
            if any(len(tag) > size and tag.lower().startswith(suffix) for tag in tags):
                return size
        return 0

    @classmethod
    def _strip_orphan_close_tags(cls, text: str) -> str:
        if "</" not in text:
            return text
        lower = text.lower()
        out: list[str] = []
        idx = 0
        while idx < len(text):
            matched = False
            for tag in cls._CLOSE_TAGS:
                tag_lower = tag.lower()
                if lower.startswith(tag_lower, idx):
                    idx += len(tag)
                    while idx < len(text) and text[idx] in " \t\r\n":
                        idx += 1
                    matched = True
                    break
            if not matched:
                out.append(text[idx])
                idx += 1
        return "".join(out)


class StreamingContextScrubber:
    """Remove streamed ``<memory-context>`` spans across split deltas."""

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span = False
        self._buf = ""

    def reset(self) -> None:
        self._in_span = False
        self._buf = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            lower = buf.lower()
            if self._in_span:
                idx = lower.find(self._CLOSE_TAG)
                if idx == -1:
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[idx + len(self._CLOSE_TAG):]
                self._in_span = False
                continue

            idx = lower.find(self._OPEN_TAG)
            if idx == -1:
                held = self._max_partial_suffix(buf, self._OPEN_TAG)
                if held:
                    out.append(buf[:-held])
                    self._buf = buf[-held:]
                else:
                    out.append(buf)
                return "".join(out)

            out.append(buf[:idx])
            buf = buf[idx + len(self._OPEN_TAG):]
            self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        lower = buf.lower()
        tag_lower = tag.lower()
        max_check = min(len(lower), len(tag_lower) - 1)
        for size in range(max_check, 0, -1):
            if tag_lower.startswith(lower[-size:]):
                return size
        return 0


class StreamingSentinelScrubber:
    """Suppress upstream-gateway error strings without leaking split fragments.

    A gateway error (see :data:`UPSTREAM_ERROR_SENTINELS`) replaces the whole
    message content, so it always begins at the start of the turn.  This
    scrubber holds back content only while the accumulated text is still a
    prefix of a known sentinel:

      * the moment it matches a full sentinel, the turn is flagged
        (:attr:`matched`) and the rest of the message is swallowed;
      * the moment it diverges from every sentinel, it flushes the held text
        and passes everything through verbatim thereafter.

    Because holding stops at the first divergence, a legitimate message pays at
    most a few characters of buffering (e.g. ``"⚠️ Note: ..."`` diverges from
    ``"⚠️ 上游..."`` immediately) and is never altered.
    """

    def __init__(self, sentinels: tuple[str, ...] = UPSTREAM_ERROR_SENTINELS) -> None:
        self._sentinels: tuple[str, ...] = tuple(sentinels)
        self._buf = ""
        self._passthrough = False
        self._matched = False

    @property
    def matched(self) -> bool:
        """Whether the turn's content was a suppressed gateway error string."""
        return self._matched

    def reset(self) -> None:
        self._buf = ""
        self._passthrough = False
        self._matched = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self._matched:
            # Whole turn is a gateway error -- swallow any trailing fragments.
            return ""
        if self._passthrough:
            return text

        self._buf += text
        candidate = self._buf.lstrip()
        if not candidate:
            # Only leading whitespace so far -- keep holding; it may precede a
            # sentinel and will be emitted verbatim on divergence/flush.
            return ""

        if any(candidate.startswith(sentinel) for sentinel in self._sentinels):
            self._matched = True
            self._buf = ""
            return ""

        if any(sentinel.startswith(candidate) for sentinel in self._sentinels):
            # Still a proper prefix of some sentinel -- keep holding.
            return ""

        # Diverged: real content.  Flush the buffer and stop matching.
        self._passthrough = True
        out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        if self._matched:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        candidate = out.lstrip()
        if (
            _contains_cjk(candidate)
            and any(sentinel.startswith(candidate) for sentinel in self._sentinels)
        ):
            # Stream ended mid-sentinel while still holding a gateway-error
            # prefix that already contains CJK -- a truncated error.  Suppress
            # it so no partial Chinese ever reaches the user.  A lone shared
            # lead-in (e.g. "⚠️") has no CJK and is emitted normally.
            self._matched = True
            return ""
        return out
