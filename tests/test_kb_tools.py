"""Knowledge-base tools fail closed when session agent identity is missing."""
from __future__ import annotations


import pytest

from surogates.db import ops_engine
from surogates.tools.builtin import kb_tools


async def test_kb_list_pages_fails_closed_without_agent_id(monkeypatch):
    """The handler resolves agent_id from its kwargs and fails closed
    when the dispatch context carries none -- never silently reading the
    env var instead."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    monkeypatch.delenv("SUROGATES_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_list_pages_handler({"kb_id": "some-kb"})


async def test_kb_read_page_fails_closed_without_agent_id(monkeypatch):
    """Same contract for kb_read_page."""
    monkeypatch.setattr(ops_engine, "_session_factory", None)
    monkeypatch.delenv("SUROGATES_AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="agent_id"):
        await kb_tools._kb_read_page_handler(
            {"kb_id": "some-kb", "path": "index.md"},
        )
