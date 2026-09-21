"""The board's LLM admission gate is always on and fails closed.

Everything visible on a board is meant to have passed this gate, which
is why there is no deterministic fallback: a verifier that cannot answer
rejects, and the writer retries on a later turn.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from surogates.board.verifier import (
    VERIFICATION_UNAVAILABLE,
    NoteDraft,
    verify_notes_llm,
)

pytestmark = pytest.mark.asyncio


def _llm(reply: str | None = None, *, raises: BaseException | None = None):
    """An LLM client double whose reply (or failure) the test chooses."""

    async def _create(**_kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=reply)),
        ])

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
    )


def _drafts(n: int = 2) -> list[NoteDraft]:
    return [
        NoteDraft(type="finding", content=f"note {i}") for i in range(n)
    ]


async def _verify(drafts, client, *, timeout_seconds: float = 5.0):
    return await verify_notes_llm(
        drafts, llm_client=client, model="test-model",
        timeout_seconds=timeout_seconds,
    )


async def test_a_verifier_error_rejects_the_whole_batch():
    kept, rejected = await _verify(
        _drafts(), _llm(raises=RuntimeError("upstream is down")),
    )

    assert kept == []
    assert rejected == [(0, VERIFICATION_UNAVAILABLE), (1, VERIFICATION_UNAVAILABLE)]


async def test_a_verifier_timeout_rejects_the_whole_batch():
    async def _hang(**_kwargs):
        await asyncio.sleep(10)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_hang)),
    )

    kept, rejected = await _verify(_drafts(1), client, timeout_seconds=0.05)

    assert kept == []
    assert rejected == [(0, VERIFICATION_UNAVAILABLE)]


async def test_an_unparseable_reply_rejects_the_whole_batch():
    kept, rejected = await _verify(_drafts(1), _llm("not json at all"))

    assert kept == []
    assert rejected == [(0, VERIFICATION_UNAVAILABLE)]


async def test_a_note_the_verifier_did_not_rule_on_is_not_admitted():
    """Silence is not consent: only the note with a verdict gets in."""
    kept, rejected = await _verify(
        _drafts(2), _llm('[{"index": 0, "keep": true}]'),
    )

    assert [d.content for d in kept] == ["note 0"]
    assert rejected == [(1, VERIFICATION_UNAVAILABLE)]


async def test_a_rejection_carries_the_verifiers_reason():
    kept, rejected = await _verify(
        _drafts(1),
        _llm('[{"index": 0, "keep": false, "reason": "unsupported claim"}]'),
    )

    assert kept == []
    assert rejected == [(0, "unsupported claim")]


async def test_a_fenced_json_reply_is_still_read():
    """Models fence their JSON; the gate must not reject good verdicts
    for punctuation."""
    kept, rejected = await _verify(
        _drafts(1), _llm('```json\n[{"index": 0, "keep": true}]\n```'),
    )

    assert [d.content for d in kept] == ["note 0"]
    assert rejected == []
