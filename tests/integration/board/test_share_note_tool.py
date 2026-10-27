"""share_note tool handler end-to-end (fake LLM verifier client)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.board.tools import _share_note_handler
from surogates.db.models import BoardNote
from surogates.session.events import EventType


def _approving_client(prompts: list[str] | None = None):
    """Fake OpenAI-style client whose verifier approves every candidate."""
    client = AsyncMock()

    async def _create(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        if prompts is not None:
            prompts.append(prompt)
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


async def _ran(session_store, session, tool: str, output: str) -> int:
    """A tool result in *session*, as the harness records one."""
    return await session_store.emit_event(
        session.id, EventType.TOOL_RESULT,
        {"tool_call_id": f"tc-{tool}", "name": tool, "content": output},
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_share_note_admits_and_emits_event(
    parent_session, session_factory, session_store, org_id,
):
    group_id = parent_session.id
    await _ran(session_store, parent_session, "read_file",
               "30: seen = {_norm(n.content) for n in notes}  # dedupe")
    result = json.loads(await _share_note_handler(
        {"notes": [
            {"type": "FACT", "content": "render.py:30 dedupes by norm content",
             "ref": {"kind": "tool_result", "tool": "read_file"}},
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
    await _ran(session_store, parent_session, "terminal", "f.py:1 raises KeyError")
    result = json.loads(await _share_note_handler(
        {"notes": [{"type": "FACT", "content": "a concrete fact f.py:1",
                    "ref": {"kind": "tool_result", "tool": "terminal"}}]},
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
    await _ran(session_store, child, "terminal", "r.py:9: ImportError: no module q")
    result = json.loads(await _share_note_handler(
        {"notes": [{"type": "FAIL", "content": "path q dead-ends at r.py:9",
                    "ref": {"kind": "tool_result", "tool": "terminal"}}]},
        **kwargs,
    ))
    label = result["admitted"][0]["writer_label"]
    assert label == "w" + child.id.hex[:4]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_uncited_note_is_refused_and_a_claim_needs_no_source(
    parent_session, session_factory, session_store,
):
    result = json.loads(await _share_note_handler(
        {"notes": [
            {"type": "FACT", "content": "store.py:10 caches settings"},
            {"type": "RESULT", "content": "outcome=ok|evidence=12 passed|risk=-"},
            {"type": "CLAIM", "content": "taking the slack adapter"},
        ]},
        **_kwargs(parent_session, session_factory, session_store, parent_session.id),
    ))
    assert [n["type"] for n in result["admitted"]] == ["CLAIM"]
    reasons = [r["reason"] for r in result["rejected"]]
    assert "FACT needs a ref" in reasons[0] and "RESULT needs a ref" in reasons[1]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_verifier_judges_a_note_against_the_source_it_cites(
    parent_session, session_factory, session_store,
):
    """The gate used to see the note's wording alone, so a well-phrased
    "evidence" nobody ran was admitted.  It now reads what the ref holds."""
    prompts: list[str] = []
    kwargs = _kwargs(parent_session, session_factory, session_store, parent_session.id)
    kwargs["llm_client"] = _approving_client(prompts)
    await _ran(session_store, parent_session, "terminal", "== 3 failed, 9 passed ==")
    event_id = await _ran(session_store, parent_session, "terminal", "== 12 passed in 4.1s ==")
    result = json.loads(await _share_note_handler(
        {"notes": [{
            "type": "RESULT", "content": "outcome=fixed|evidence=pytest 12 passed|risk=-",
            "ref": {"kind": "tool_result", "tool": "terminal", "contains": "12 passed"},
        }]},
        **kwargs,
    ))
    assert result["admitted"], result
    [prompt] = prompts
    assert "SOURCE:\n   | == 12 passed in 4.1s ==" in prompt
    assert "3 failed" not in prompt
    async with session_factory() as db:
        note = await db.get(BoardNote, result["admitted"][0]["id"])
    assert note.ref == {"kind": "event", "session_id": str(parent_session.id), "event_id": event_id}


@pytest.mark.asyncio(loop_scope="session")
async def test_a_ref_that_cannot_be_followed_is_refused_before_the_verifier(
    parent_session, session_factory, session_store,
):
    import uuid

    from surogates.session.provisioning import create_child_session

    prompts: list[str] = []
    sibling = await create_child_session(
        store=session_store, parent=parent_session, channel="worker", model=None,
        config={"context_group_id": str(parent_session.id)},
    )
    sibling_event = await _ran(session_store, sibling, "terminal", "== 5 passed ==")
    kwargs = _kwargs(parent_session, session_factory, session_store, parent_session.id)
    kwargs["llm_client"] = _approving_client(prompts)
    result = json.loads(await _share_note_handler(
        {"notes": [
            {"type": "FACT", "content": "outsider fact z.py:1",
             "ref": {"kind": "event", "session_id": str(uuid.uuid4()), "event_id": 1}},
            {"type": "RESULT", "content": "outcome=ok|evidence=5 passed|risk=-",
             "ref": {"kind": "event", "session_id": str(sibling.id), "event_id": sibling_event}},
            {"type": "FAIL", "content": "never ran this tool x.py:2",
             "ref": {"kind": "tool_result", "tool": "web_fetch"}},
        ]},
        **kwargs,
    ))
    assert result["admitted"] == []
    reasons = {r["index"]: r["reason"] for r in result["rejected"]}
    assert "ref target not accessible" in reasons[0]
    assert "own session" in reasons[1]
    assert "no result of web_fetch" in reasons[2]
    assert prompts == []  # nothing uncited reaches the verifier
