"""Object key prefix helper for prefix-scoped workspace layouts.

Used by storage call sites that need to translate a logical key
(e.g., ``sessions/{id}/screenshot.png``) into the actual S3-compatible
object key (e.g., ``{project_id}/{agent_id}/sessions/{id}/screenshot.png``).

Empty prefix is a valid no-op for tests and any backend that uses
bucket-level isolation rather than prefix scoping.
"""

from __future__ import annotations


def prefixed(key: str, prefix: str) -> str:
    """Prepend ``prefix`` to ``key`` with a single ``/`` separator.

    ``prefix`` may have a trailing ``/`` — it is normalised away.
    ``key`` may have a leading ``/`` — it is normalised away.
    A trailing ``/`` on ``key`` is preserved so workspace prefixes
    keep their listing semantics.

    Empty ``prefix`` returns ``key`` unchanged. Empty ``key`` returns
    ``prefix`` (with its own trailing slash stripped).
    """
    p = prefix.rstrip("/")
    if not p:
        return key
    # Strip only a single leading slash; preserve the rest of the path
    # (and the trailing slash, which carries listing semantics).
    k = key[1:] if key.startswith("/") else key
    if not k:
        return p
    return f"{p}/{k}"
