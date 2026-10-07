"""A thread's copy, for the harness tools that reach a project's files through object storage.

Such a tool runs in the worker, not in the pod: ``vision_analyze``, the
media generators, the browser's screenshots and the vision inject.  When
the session's pod holds a thread's copy, they read and write the copy
through the pod's ``_file`` command instead, so they see the thread's own
changes and what they write lands with its turn.  A thread with no pod yet
has changed nothing, and its copy would be the real files: a read there
goes to storage as before.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from surogates.sandbox.base import SandboxUnavailableError
from surogates.sandbox.pool import SandboxPool
from surogates.workstreams import is_project_thread


def has_copy(sandbox_pool: Any, owner: Any) -> bool:
    """Whether *owner*'s pod is up and holds a thread's copy."""
    return isinstance(sandbox_pool, SandboxPool) and owner is not None and sandbox_pool.holds_copy(str(owner))


def writes_to_copy(sandbox_pool: Any, owner: Any, session_config: dict[str, Any] | None) -> bool:
    """Whether a file a harness tool makes goes to a thread's copy, or nowhere.

    It does over a pod that holds a copy, a thread's delegate child's
    included, and for a thread that works on one, whose pod may be gone:
    a project's thread with a storage bucket.  A thread with none has the
    real files for its workspace, as any other session.
    """
    config = session_config or {}
    return has_copy(sandbox_pool, owner) or (bool(config.get("storage_bucket")) and is_project_thread(config))


async def _file(sandbox_pool: SandboxPool, owner: Any, request: dict[str, Any], done: str) -> dict[str, Any]:
    """The pod's answer to *request*, which holds *done* when it worked; ValueError when not."""
    try:
        result = json.loads(await sandbox_pool.execute(str(owner), "_file", json.dumps(request)))
    except (SandboxUnavailableError, ValueError) as exc:  # the pod gone or never there; an answer not JSON
        raise ValueError(str(exc)) from exc
    if isinstance(result, dict) and done in result:
        return result
    # The pod's error, or a timeout or an HTTP failure, which carry none.
    said = (result.get("error") or result.get("stderr")) if isinstance(result, dict) else None
    raise ValueError(said or "The thread's pod gave no answer")


async def read_copy(sandbox_pool: SandboxPool, owner: Any, path: str) -> bytes:
    """*path* from the copy in *owner*'s pod; ValueError when it cannot be read."""
    result = await _file(sandbox_pool, owner, {"action": "read", "path": path}, "content_b64")
    return base64.b64decode(result["content_b64"])


async def write_copy(sandbox_pool: SandboxPool, owner: Any, path: str, data: bytes) -> None:
    """Write *path* into the copy in *owner*'s pod; ValueError when it cannot be written."""
    if not has_copy(sandbox_pool, owner):
        raise ValueError("The thread's pod holds no copy of the project's files")
    request = {"action": "write", "path": path, "content_b64": base64.b64encode(data).decode()}
    await _file(sandbox_pool, owner, request, "ok")
