"""Tests for the thinking-runaway mitigation: timeout bump, heartbeat,
in-stream runaway detection, and retry-with-thinking-off.

Each test exercises one layer in isolation; the runaway-retry test
(test_runaway_retry_disables_thinking) covers the end-to-end glue.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import (
    call_llm_streaming_inner,
    compute_stream_stale_timeout,
)
from surogates.session.events import EventType


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        config={"temperature": 0.7},
        model="zai-org/GLM-5.1",
    )


# ---------------------------------------------------------------------------
# Task 2: conditional stale-timeout bump for reasoning models
# ---------------------------------------------------------------------------


def test_stream_stale_timeout_bumped_for_reasoning_models(monkeypatch):
    """Reasoning models get a 600s default so long silent reasoning
    phases on DeepInfra do not trip the watchdog."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
    )

    assert timeout == 600.0


def test_stream_stale_timeout_unchanged_for_non_reasoning_models(monkeypatch):
    """OpenAI/Anthropic and other non-toggle models keep the 180s default."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 180.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", False,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short request"}],
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
    )

    assert timeout == 180.0


def test_stream_stale_timeout_explicit_override_wins_for_reasoning(monkeypatch):
    """SUROGATES_STREAM_STALE_TIMEOUT env var must override the reasoning bump."""
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT", 90.0,
    )
    monkeypatch.setattr(
        "surogates.harness.llm_call.STREAM_STALE_TIMEOUT_EXPLICIT", True,
    )

    timeout = compute_stream_stale_timeout(
        [{"role": "user", "content": "short"}],
        base_url="https://api.deepinfra.com/v1/openai",
        model="zai-org/GLM-5.1",
        explicit_timeout=90.0,
    )

    assert timeout == 90.0
