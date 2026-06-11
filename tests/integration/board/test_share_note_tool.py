"""share_note tool handler end-to-end (fake LLM verifier client)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.board.tools import _share_note_handler
from surogates.db.models import BoardNote
from surogates.session.events import EventType


def _approving_client():
    """Fake OpenAI-style client whose verifier approves every candidate."""
    client = AsyncMock()

    async def _create(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        count = sum(
            1 for line in prompt.splitlines()
            if line.split(":")[0].isdigit()
        )
        verdicts = [
            {"index": i, "keep": True, "reason": ""} for i in range(count)
        ]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(verdicts)))])

    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


def _kwargs(session, session_factory, session_store, group_id):
    return dict(
        session_id=str(session.id),
        session_factory=session_factory,
        session_store=session_store,
        tenant=SimpleNamespace(org_id=session.org_id),
        session_config={"context_group_id": str(group_id)},
        llm_client=_approving_client(),
        model="main-model",
        summary_llm_client=None,  # falls back to llm_client
        summary_model=None,
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_share_note_admits_and_emits_event(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    result = json.loads(await _share_note_handler(
        {"notes": [
            {"type": "FACT", "content": "render.py:30 dedupes by norm content"},
        ]},
        **_kwargs(parent_session, session_factory, session_store, group_id),
    ))
    assert result["admitted"] and not result["rejected"]
    assert result["admitted"][0]["type"] == "FACT"
    assert result["admitted"][0]["id"]

    async with session_factory() as db:
        note = await db.get(BoardNote, result["admitted"][0]["id"])
    assert note is not None
    assert note.writer_label == "coord"  # writer is the group root

    events = await session_store.get_events(
        parent_session.id, types=[EventType.BOARD_NOTE],
    )
    assert events and events[-1].data["notes"][0]["id"] == note.id


@pytest.mark.asyncio(loop_scope="session")
async def test_share_note_requires_group_membership(
    parent_session, session_factory, session_store,
):
    kwargs = _kwargs(parent_session, session_factory, session_store,
                     parent_session.id)
    kwargs["session_config"] = {}
    result = json.loads(await _share_note_handler(
        {"notes": [{"type": "FACT", "content": "x"}]}, **kwargs,
    ))
    assert "error" in result


@pytest.mark.asyncio(loop_scope="session")
async def test_share_note_fail_closed_when_verifier_down(
    parent_session, session_factory, session_store,
):
    kwargs = _kwargs(parent_session, session_factory, session_store,
                     parent_session.id)
    broken = AsyncMock()
    broken.chat.completions.create = AsyncMock(side_effect=RuntimeError("down"))
    kwargs["llm_client"] = broken
    result = json.loads(await _share_note_handler(
        {"notes": [{"type": "FACT", "content": "a concrete fact f.py:1"}]},
        **kwargs,
    ))
    assert not result["admitted"]
    assert "verification unavailable" in result["rejected"][0]["reason"]


@pytest.mark.asyncio(loop_scope="session")
async def test_share_note_worker_label_is_uuid_derived(
    parent_session, session_factory, session_store,
):
    # A non-root writer in the same group gets a w<hex4> label.
    from surogates.session.provisioning import create_child_session
    child = await create_child_session(
        store=session_store, parent=parent_session, channel="worker",
        model=None,
        config={"context_group_id": str(parent_session.id)},
    )
    kwargs = _kwargs(child, session_factory, session_store, parent_session.id)
    result = json.loads(await _share_note_handler(
        {"notes": [{"type": "FAIL", "content": "path q dead-ends at r.py:9"}]},
        **kwargs,
    ))
    label = result["admitted"][0]["writer_label"]
    assert label == "w" + child.id.hex[:4]
