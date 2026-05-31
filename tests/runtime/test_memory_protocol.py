"""Tests for the memory R2 key layout + envelope shape.

The canonical R2 key for per-user memory is
``users/<user_id>/memory.json`` (under the agent's
``ctx.storage_key_prefix``).  Memory contents land inside a JSON
envelope so version + content travel together atomically — no
partial-read scenario where the version metadata desyncs from the
content.
"""

from __future__ import annotations

import json

import pytest


def test_memory_object_key_user_session():
    from surogates.runtime.memory_protocol import memory_object_key

    assert memory_object_key(
        storage_key_prefix="p-1/a-1", user_id="u-99",
    ) == "p-1/a-1/users/u-99/memory.json"


def test_memory_object_key_service_account_session():
    """Service-account sessions have no user_id; the survey notes
    the legacy disk path uses ``shared/memory`` for them.  Plan 4
    keys the SA path as ``shared/memory.json`` under the same
    storage_key_prefix."""
    from surogates.runtime.memory_protocol import memory_object_key

    assert memory_object_key(
        storage_key_prefix="p-1/a-1", user_id=None,
    ) == "p-1/a-1/shared/memory.json"


def test_memory_envelope_encode_decode_round_trip():
    from surogates.runtime.memory_protocol import (
        _MemoryEnvelope, decode_envelope, encode_envelope,
    )

    env = _MemoryEnvelope(version=42, content="hello world")
    raw = encode_envelope(env)
    decoded = decode_envelope(raw)
    assert decoded == env


def test_memory_envelope_decode_rejects_non_json():
    from surogates.runtime.memory_protocol import (
        EnvelopeDecodeError, decode_envelope,
    )

    with pytest.raises(EnvelopeDecodeError):
        decode_envelope(b"not json at all")


def test_memory_envelope_decode_rejects_missing_keys():
    from surogates.runtime.memory_protocol import (
        EnvelopeDecodeError, decode_envelope,
    )

    with pytest.raises(EnvelopeDecodeError):
        decode_envelope(json.dumps({"version": 1}).encode())
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope(json.dumps({"content": "x"}).encode())


def test_memory_envelope_decode_rejects_wrong_types():
    from surogates.runtime.memory_protocol import (
        EnvelopeDecodeError, decode_envelope,
    )

    # version must be int, not string
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope(json.dumps({"version": "1", "content": "x"}).encode())
    # content must be string, not None
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope(json.dumps({"version": 1, "content": None}).encode())
