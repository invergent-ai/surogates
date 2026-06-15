"""``/compress`` must not crash when history holds multimodal messages.

Pins the PROD bug where ``/compress`` raised
``AttributeError: 'list' object has no attribute 'strip'`` inside
``_handle_compress_command``.  The handler stripped the ``/compress``
message out of the history with ``(m.get("content") or "").strip() ==
"/compress"``, which assumed every user message carries *string*
content.  A multimodal user message (text blocks + images) carries a
*list*, so ``(list or "")`` is the list and ``.strip()`` blew up — taking
down the whole wake() before compression could run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


def _harness(store, compressor) -> AgentHarness:
    tenant = TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )
    return AgentHarness(
        session_store=store,
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=compressor,
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        vision_client=None,
        vision_model="",
    )


@pytest.mark.asyncio
async def test_compress_with_multimodal_history_does_not_crash() -> None:
    store = MagicMock()
    store.emit_event = AsyncMock()

    compressor = MagicMock(spec=ContextCompressor)
    compressor.context_length = 8000
    compressor.compress = AsyncMock(
        return_value=([{"role": "user", "content": "kept"}], {"strategy": "halve"})
    )

    harness = _harness(store, compressor)
    session = SimpleNamespace(id=uuid4())

    # A realistic history: enough messages to clear the >5 guard, including
    # one multimodal user message whose content is a *list* of blocks, and
    # the trailing "/compress" command itself.
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        },
        {"role": "assistant", "content": "nice picture"},
        {"role": "user", "content": "tell me more"},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "/compress"},
    ]

    # Before the fix this raises AttributeError on the list content.
    await harness._handle_compress_command(
        session, messages, system_prompt="sys", lease=MagicMock()
    )

    # Compression ran (history was large enough), and the /compress turn
    # was removed before handing the messages to the compressor.
    compressor.compress.assert_awaited_once()
    passed_messages = compressor.compress.await_args.args[0]
    assert {"role": "user", "content": "/compress"} not in passed_messages
    store.emit_event.assert_awaited()
