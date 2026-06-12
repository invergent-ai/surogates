"""Tests for knowledge-base tool agent_id resolution.

The bug: ``kb_list_pages`` / ``kb_read_page`` resolved ``agent_id`` from
the ``SUROGATES_AGENT_ID`` env var, which is only set in helm mode (one
pod per agent). In the shared runtime one worker serves many agents and
that env var is unset, so KB tools failed for every shared-runtime
session with "SUROGATES_AGENT_ID is not set".

The fix resolves ``agent_id`` from the per-session tool-dispatch kwargs
(``agent_id=session.agent_id``, passed by ``harness.tool_exec``) instead
of from the process environment.
"""
from __future__ import annotations

import pytest

from surogates.db import ops_engine
from surogates.tools.builtin import kb_tools

AGENT_ID = "43196a20-7af0-48c0-a355-3e3a03545f66"


def test_agent_id_resolved_from_kwargs():
    """The per-session agent_id arrives in the handler kwargs."""
    assert kb_tools._agent_id_from_kwargs({"agent_id": AGENT_ID}) == AGENT_ID


def test_agent_id_missing_from_context_raises():
    """No agent_id in the dispatch context is a wiring bug -- fail loud,
    with a message that points at the context, not an operator env var."""
    with pytest.raises(RuntimeError, match="agent_id"):
        kb_tools._agent_id_from_kwargs({})


def test_agent_id_does_not_fall_back_to_env(monkeypatch):
    """Resolution must NOT read SUROGATES_AGENT_ID: in the shared runtime
    it is unset, and depending on it is exactly the bug we fixed. Even
    when the env var is present it must be ignored."""
    monkeypatch.setenv("SUROGATES_AGENT_ID", "env-agent-must-be-ignored")
    with pytest.raises(RuntimeError, match="agent_id"):
        kb_tools._agent_id_from_kwargs({})


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


def test_agent_knowledge_bases_read_model_has_mode_column():
    """The read-side M2M mirrors the writer-side mode column so the
    worker can SELECT it when loading attached KBs."""
    from surogates.db.ops_models import agent_knowledge_bases

    assert "mode" in agent_knowledge_bases.c
