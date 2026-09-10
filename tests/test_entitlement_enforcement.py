"""Knowledge-base tools reject reads outside the current user's purchased plan."""

from __future__ import annotations


import pytest


@pytest.mark.asyncio
async def test_kb_read_refuses_a_kb_outside_the_plan():
    from surogates.tools.builtin.kb_tools import _kb_read_page_handler

    result = await _kb_read_page_handler(
        {"kb_id": "kb-locked", "path": "index.md"},
        agent_id="a1",
        session_config={"entitlements": {"kb_ids": ["kb-open"]}},
    )
    assert "not included in the current user's plan" in result


@pytest.mark.asyncio
async def test_kb_list_refuses_a_kb_outside_the_plan():
    from surogates.tools.builtin.kb_tools import _kb_list_pages_handler

    result = await _kb_list_pages_handler(
        {"kb_id": "kb-locked"},
        agent_id="a1",
        session_config={"entitlements": {"kb_ids": []}},
    )
    assert "not included in the current user's plan" in result
