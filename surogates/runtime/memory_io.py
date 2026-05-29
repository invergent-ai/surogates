"""R2 read/write wrappers for per-user memory.

Plan 4 / Tasks 7+8.  Thin async functions over the storage
backend that encode/decode the JSON envelope and surface the
version field to the caller.

A missing-key read returns ``None`` so the caller can branch on
"new user, no prior memory".  A corrupted envelope (non-JSON,
wrong schema) also returns ``None`` so a botched manual migration
doesn't crash session bootstrap — the next write reseeds the
object.

Writes always succeed and always increment the version.  Conflict
detection is the caller's responsibility (compare the version
returned by ``read_user_memory`` at session start with what's on
R2 at write time — see ``R2MemoryStore`` in Task 10).
"""

from __future__ import annotations

from typing import Any

from surogates.runtime.memory_protocol import (
    EnvelopeDecodeError,
    _MemoryEnvelope,
    decode_envelope,
    encode_envelope,
)

__all__ = ["read_user_memory", "write_user_memory"]


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

    Plan 4 / Task 9 — the content is run through the same
    injection scan as SOUL.md / AGENT.md (Plan 3 Task 10) before
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
