"""Worker-side ``read_file`` branch for image paths.

The K8s sandbox does not carry LLM clients or vision configuration, so
the worker intercepts ``read_file(image.png)`` calls before they reach
sandbox dispatch and routes them through ``vision_analyze``.  The
``vision_analyze`` JSON envelope is reshaped into a ``read_file`` result
so the LLM never sees ``vision_analyze`` output unless it called that
tool explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ENTRIES = 8
_DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_CACHE_DISABLED_ENV = "READ_IMAGE_CACHE_DISABLED"


class _ImageCache:
    """In-memory LRU.  Worker-process scoped."""

    def __init__(
        self,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_entry_bytes: int = _DEFAULT_MAX_ENTRY_BYTES,
    ) -> None:
        self._max_entries = max_entries
        self._max_entry_bytes = max_entry_bytes
        self._lock = threading.Lock()
        self._store: OrderedDict[tuple, str] = OrderedDict()

    def get(self, key: tuple) -> str | None:
        with self._lock:
            if key not in self._store:
                return None
            value = self._store.pop(key)
            self._store[key] = value
            return value

    def put(self, key: tuple, value: str) -> None:
        if len(value.encode("utf-8")) > self._max_entry_bytes:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_CACHE = _ImageCache()


def _build_key(path: str, kwargs: dict[str, Any]) -> tuple | None:
    """Build a cache key for ``path``.

    Local workspaces key on ``(abs_path, mtime_ns, size)``; storage-backed
    workspaces fall back to ``(session_id, workspace_key)`` so multiple
    reads of the same uploaded image within a session share an entry.
    Returns ``None`` if neither shape is reachable (the cache is then
    bypassed).
    """
    if os.path.exists(path):
        try:
            st = os.stat(path)
            return ("local", os.path.realpath(path), st.st_mtime_ns, st.st_size)
        except OSError:
            return None
    session_id = kwargs.get("session_id")
    if session_id is not None:
        return ("storage", session_id, path)
    return None


async def handle_image_read(
    path: str,
    arguments: dict[str, Any],
    dispatch: Callable[..., Awaitable[str]],
    kwargs: dict[str, Any],
) -> str:
    """Run ``vision_analyze`` on the image and render the result as ``read_file``.

    ``dispatch`` is the worker's ``ToolRegistry.dispatch`` (or any callable
    with the same signature).  Tests inject a stub.  The returned JSON is
    the same shape ``_handle_text`` / ``_handle_document`` produce.
    """
    # Import lazily — file_ops pulls in the whole tool registry.
    from surogates.tools.builtin.file_ops import (
        _apply_line_window,
        _render_read_window,
        get_max_lines,
    )

    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", get_max_lines()), get_max_lines())

    cache_disabled = os.environ.get(_CACHE_DISABLED_ENV) == "1"
    key = None if cache_disabled else _build_key(path, kwargs)

    analysis: str | None = None
    cached_hit = False
    if key is not None:
        analysis = _CACHE.get(key)
        cached_hit = analysis is not None

    if analysis is None:
        raw = await dispatch("vision_analyze", {"image": path}, **kwargs)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({
                "error": (
                    f"vision_analyze returned non-JSON output for '{path}': "
                    f"{raw[:200]}"
                ),
            }, ensure_ascii=False)
        if not isinstance(parsed, dict):
            return json.dumps({
                "error": (
                    f"vision_analyze returned unexpected payload type "
                    f"({type(parsed).__name__}) for '{path}'."
                ),
            }, ensure_ascii=False)
        if "error" in parsed:
            return json.dumps({
                "error": (
                    f"read_file could not analyze image '{path}': "
                    f"{parsed['error']}"
                ),
            }, ensure_ascii=False)
        analysis = parsed.get("analysis") or ""
        if key is not None and analysis:
            _CACHE.put(key, analysis)

    filename = Path(path).name
    markdown = f"# Image: {filename}\n\n{analysis}\n"
    lines = markdown.splitlines(keepends=True)

    selected, total_lines, _start, _end, _truncated = _apply_line_window(
        lines, offset, limit,
    )

    content, lines_shown, next_offset = _render_read_window(
        selected, offset, total_lines,
    )
    truncated = next_offset is not None

    logger.info(
        "event=image.analyze path=%s bytes=%d cached=%s",
        path, len(analysis), cached_hit,
    )

    result: dict[str, Any] = {
        "content": content,
        "path": path,
        "total_lines": total_lines,
        "lines_shown": lines_shown,
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
    }
    if next_offset is not None:
        result["next_offset"] = next_offset
    return json.dumps(result, ensure_ascii=False)
