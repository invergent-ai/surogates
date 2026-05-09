"""Stateful scrubbers for provider text streamed across delta boundaries."""

from __future__ import annotations

from typing import ClassVar


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
