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
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ENTRIES = 8
_DEFAULT_MAX_ENTRY_BYTES = 2 * 1024 * 1024
_CACHE_DISABLED_ENV = "READ_IMAGE_CACHE_DISABLED"


class _ImageCache:
    """In-memory LRU.  Worker-process scoped: every key names its session (see :func:`_build_key`)."""

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


async def _build_key(path: str, kwargs: dict[str, Any]) -> tuple | None:
    """The cache key of *path* as this session's ``vision_analyze`` would read it, or ``None``.

    The key names the session, the image's place in its workspace and this
    version of the image, so a session never gets another session's analysis
    and a changed image is analyzed anew, whether the path given was relative
    or absolute.  The path is checked first, as ``vision_analyze`` checks it:
    nothing is stat'ed, resolved or looked up in the cache for a path it
    refuses, and such a path has no key (``vision_analyze`` then refuses it).

    ``None`` too, so the cache is bypassed, without a session, for a read
    through a ``workspace_io``, for a URL (no version to key on), for a
    thread's copy of the workspace, and wherever the image cannot be stat'ed.
    """
    session_id = kwargs.get("session_id")
    # The reference vision_analyze reads: it strips the path it is given.
    ref = path.strip()
    if session_id is None or not ref or kwargs.get("workspace_io") is not None:
        return None
    try:
        scheme = urlparse(ref).scheme
    except ValueError:  # vision_analyze refuses it in its own words
        return None
    if ref.startswith("data:") or scheme in {"http", "https"}:
        return None

    # Lazy, as file_ops below: these pull in the tool registry.
    from surogates.sandbox.copy_files import has_copy
    from surogates.storage.tenant import boundary_workspace_key, workspace_session_shim
    from surogates.tools.builtin.vision import _validate_workspace_storage_path
    from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError, validate_path
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    workspace_path = kwargs.get("workspace_path")
    storage = kwargs.get("storage")
    session_config = kwargs.get("session_config")
    try:
        bucket = (session_config or {}).get("storage_bucket")
        if storage is not None and bucket:
            # The session's object in its bucket, as vision_analyze reads it.
            relative = _validate_workspace_storage_path(ref, workspace_path=workspace_path)
            if has_copy(kwargs.get("sandbox_pool"), kwargs.get("task_id")):
                return None
            object_key = boundary_workspace_key(
                session_config,
                workspace_session_shim(session_config, session_id),
                session_id,
                relative,
            )
            info = await storage.stat(bucket, object_key)
            return (
                "storage", str(session_id), bucket, object_key,
                info.get("size"), str(info.get("modified")), info.get("etag"),
            )
        # A file of this host's workspace: contained first (no workspace, or a
        # path out of it, is refused here), then stat'ed as vision_analyze
        # reads it, one folder at a time with no link followed (#655).
        resolved = validate_path(workspace_path, ref)
        found = await LocalWorkspaceIO(workspace_path).stat(resolved)
    except (WorkspaceSandboxError, ValueError, KeyError, OSError):
        return None  # refused, or no such image: vision_analyze gives its own answer
    except Exception:
        # Not a refusal: still no key (an analysis is never wrongly shared for
        # want of one), but said, so a fault does not just turn the cache off.
        logger.warning("event=image.cache_key_failed path=%s", path, exc_info=True)
        return None
    if found is None or found.is_dir:
        return None
    return ("local", str(session_id), str(workspace_path), resolved, found.revision)


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
    from surogates.tools.utils.workspace_sandbox import NUL_REFUSED
    from surogates.tools.builtin.file_ops import (
        _apply_line_window,
        _render_read_window,
        get_max_lines,
    )

    if "\0" in path:
        # As read_file answers any other path with one, and before anything looks at it.
        return json.dumps({"error": NUL_REFUSED}, ensure_ascii=False)

    offset = max(arguments.get("offset", 1), 1)
    limit = min(arguments.get("limit", get_max_lines()), get_max_lines())

    cache_disabled = os.environ.get(_CACHE_DISABLED_ENV) == "1"
    key = None if cache_disabled else await _build_key(path, kwargs)

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
        # Kept only if the image is still the version the key names: one
        # changed while it was analyzed is analyzed anew on the next read.
        if key is not None and analysis and await _build_key(path, kwargs) == key:
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
