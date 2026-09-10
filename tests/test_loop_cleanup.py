"""Agent-loop compression uses the prompt token count reported by the provider."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_loop_ordering import _drive, _harness, _resp, _tool_resp


@pytest.mark.asyncio
async def test_compression_check_uses_the_reported_prompt_tokens(monkeypatch):
    seen: list[Any] = []
    h = _harness()
    h._compressor = SimpleNamespace(
        context_length=1000,
        _context_window=200_000,
        should_compress=lambda m, *a, **k: (seen.append(m), False)[1],
    )
    await _drive(h, [_tool_resp("c1"), _resp("Done.")], monkeypatch)
    assert seen == [1], seen
