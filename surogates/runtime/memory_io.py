"""R2 read/write wrappers for per-user memory.

Thin async functions over the storage
backend that encode/decode the JSON envelope and surface the
version field to the caller.

A missing-key read returns ``None`` so the caller can branch on
"new user, no prior memory".  A corrupted envelope (non-JSON,
wrong schema) also returns ``None`` so a botched manual migration
doesn't crash session bootstrap — the next write reseeds the
object.

Writes always succeed and always increment the version.  Conflict
detection is the caller's responsibility.
"""

from __future__ import annotations

from typing import Any

from surogates.runtime.memory_protocol import (
    EnvelopeDecodeError,
    _MemoryEnvelope,
    decode_envelope,
    encode_envelope,
)

__all__ = ["delete_memory_prefix", "read_user_memory", "write_user_memory"]


async def delete_memory_prefix(
    backend: Any, *, bucket: str, prefix: str,
) -> int:
    """Delete every object whose key starts with ``prefix`` in ``bucket``.

    Used by the delete_agent cascade (surogate-
    ops side) to drop the agent's per-user memory.  Idempotent: a
    second call after the prefix is empty returns 0 without
    raising.

    Returns the number of objects deleted.

    Cross-tenant safety: callers MUST pass a fully-qualified prefix
    like ``"{project_id}/{agent_id}/"`` (trailing slash matters --
    without it ``p-1/a-1`` would match ``p-1/a-100`` too).  The
    helper does not enforce the trailing slash; that's the
    caller's contract.
    """
    keys = await backend.list(bucket, prefix)
    for key in keys:
        await backend.delete(bucket, key)
    return len(keys)


async def read_user_memory(
    backend: Any, *, bucket: str, key: str,
) -> tuple[str, int] | None:
    """Return ``(content, version)`` for the per-user memory file.

    ``None`` when the key doesn't exist OR the existing object
    fails envelope validation (treated as start-fresh)."""
    try:
        raw = await backend.read(bucket, key)
    except (KeyError, FileNotFoundError):
        return None

    try:
        env = decode_envelope(raw)
    except EnvelopeDecodeError:
        return None
    return env.content, env.version


async def write_user_memory(
    backend: Any,
    *,
    bucket: str,
    key: str,
    content: str,
    expected_version: int,
) -> int:
    """Encode + write the envelope at ``version = expected_version + 1``.

    the content is run through the same
    injection scan as SOUL.md / AGENT.md before
    persistence.  A flagged payload writes a sanitised
    ``[BLOCKED: ...]`` marker instead of the raw content so a
    compromised tool can't smuggle 'ignore previous instructions'
    into the user's memory and bypass the LLM's system prompt
    guardrails.

    The caller is responsible for conflict detection (re-read
    before write, compare version, emit ``MEMORY_CONFLICT`` audit
    on mismatch).  This helper always writes — last-write-wins.
    Returns the new version.
    """
    from surogates.harness.context_files import scan_context_content

    sanitised = scan_context_content(content, "memory")
    new_version = expected_version + 1
    env = _MemoryEnvelope(version=new_version, content=sanitised)
    await backend.write(bucket, key, encode_envelope(env))
    return new_version
