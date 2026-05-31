"""Per-user memory R2 key layout + JSON envelope.

The canonical R2 key for per-user memory is
``<storage_key_prefix>/users/<user_id>/memory.json`` (or
``<storage_key_prefix>/shared/memory.json`` for service-account
sessions that have no user_id).

Memory contents land inside a JSON envelope so the version + content
travel together atomically.  Without the envelope a partial read
could see the version metadata at one point in time but the content
at another (or vice versa, depending on which the writer flushed
first), which would let last-write-wins silently overwrite the
'wrong' version.  The envelope makes the version field intrinsic
to the content bytes.

Schema::

    {
        "version": <int>,         # monotonic per-key
        "content": <str>          # the full memory file body
    }

A malformed / non-JSON object on R2 is treated by callers as
"start fresh" — return ``_MemoryEnvelope(version=0, content="")``
— so a corrupted on-disk file (e.g. from a half-written legacy
migration) doesn't crash session bootstrap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "EnvelopeDecodeError",
    "_MemoryEnvelope",
    "decode_envelope",
    "encode_envelope",
    "memory_object_key",
]


class EnvelopeDecodeError(ValueError):
    """The envelope bytes are not a well-formed memory envelope.

    Raised by :func:`decode_envelope` on non-JSON input, missing
    required keys, or wrong field types.  Callers translate this
    into "start fresh" semantics (treat the object as if it did
    not exist)."""


@dataclass(frozen=True, slots=True)
class _MemoryEnvelope:
    version: int
    content: str


def memory_object_key(
    *, storage_key_prefix: str, user_id: str | None,
) -> str:
    """Build the R2 object key for the per-user memory file.

    ``storage_key_prefix`` comes from ``AgentRuntimeContext``.  ``user_id`` may be ``None`` for
    service-account sessions; those route to a ``shared/`` subkey
    instead of ``users/<id>/`` so principal isolation is preserved
    even when no user owns the session.
    """
    prefix = storage_key_prefix.rstrip("/")
    if user_id is None:
        return f"{prefix}/shared/memory.json"
    return f"{prefix}/users/{user_id}/memory.json"


def encode_envelope(env: _MemoryEnvelope) -> bytes:
    """Serialise the envelope to bytes ready for R2 put."""
    return json.dumps(
        {"version": env.version, "content": env.content},
        ensure_ascii=False,
    ).encode("utf-8")


def decode_envelope(raw: bytes) -> _MemoryEnvelope:
    """Parse R2 bytes into an envelope.

    Raises :class:`EnvelopeDecodeError` on any deviation from the
    expected shape — caller treats this as "start fresh"."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnvelopeDecodeError(
            f"memory object is not valid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise EnvelopeDecodeError("memory object is not a JSON object")
    version = parsed.get("version")
    content = parsed.get("content")
    if not isinstance(version, int) or isinstance(version, bool):
        raise EnvelopeDecodeError(
            "memory envelope missing or non-int version field",
        )
    if not isinstance(content, str):
        raise EnvelopeDecodeError(
            "memory envelope missing or non-string content field",
        )
    return _MemoryEnvelope(version=version, content=content)
