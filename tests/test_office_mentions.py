"""Comments that call the agent: its address, the message a mention becomes, and how the model is told."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import surogates.harness.loop as loop_module
import surogates.harness.loop_artifact_completion as completion_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.harness.loop_context_replay import build_user_message_dict
from surogates.office import turn_end
from surogates.office.mentions import agent_address, mention_message, mention_note, mention_of, mentionable_agent, unacknowledged
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.builtin import office as office_tools
from surogates.tools.registry import ToolRegistry

MENTION = {"path": "docs/Plan.docx", "thread": "15689582", "kind": "docx", "author": "Ana", "author_id": "user:1",
           "comment": "@WRITER@agent.invalid is Q1 right?"}


def test_the_agent_is_offered_by_an_address_no_mail_can_reach():
    assert agent_address("writer") == "writer@agent.invalid"
    assert agent_address("org/writer 2") == "org-writer-2@agent.invalid"
    # The editor matches a mention by /\B[@+][A-Z0-9._%+-]+@[A-Z0-9._-]+\.[A-Z]+\b/i alone.
    assert re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9._-]+\.[A-Z]+", agent_address("3f2a_9b-c.d"), re.IGNORECASE)
    assert mentionable_agent("writer", "Writer") == {"id": "agent:writer", "name": "Writer", "email": "writer@agent.invalid"}


def test_a_mention_becomes_a_message_naming_the_file_the_person_and_the_agent():
    content, metadata = mention_message(path="docs/Plan.docx", thread="15689582", kind="docx", author="Ana", author_id="user:1",
                                        comment="@WRITER@agent.invalid is Q1 right?", agent_id="writer", agent_name="Writer")
    assert content == "[Comment on Plan.docx] Ana: «@Writer is Q1 right?»"
    assert metadata == MENTION
    assert mention_of({"content": content, "source": "office_mention", "metadata": metadata}) == MENTION
    # A typed message's metadata is anyone's to write: without the route's source it is no mention.
    assert mention_of({"content": content, "metadata": metadata}) is None
    assert mention_of({"source": "office_mention", "metadata": {**metadata, "thread": ""}}) is None


CONTENT = "[Comment on Plan.docx] Ana: «@Writer is Q1 right?»"


def _event(event_id, kind, data=None):
    return SimpleNamespace(id=event_id, type=kind.value, data=data or {})


def _mention(event_id, **change):
    return _event(event_id, EventType.USER_MESSAGE, {"content": CONTENT, "source": "office_mention", "metadata": {**MENTION, **change}})


def _harness(store):
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder

    tenant = TenantContext(org_id=UUID("00000000-0000-0000-0000-000000000001"), user_id=UUID("00000000-0000-0000-0000-000000000002"),
                           org_config={}, user_preferences={}, permissions=frozenset(), asset_root="/tmp/test")
    return AgentHarness(session_store=store, tool_registry=ToolRegistry(), llm_client=AsyncMock(), tenant=tenant, worker_id="w",
                        budget=IterationBudget(max_total=10), context_compressor=MagicMock(spec=ContextCompressor),
                        prompt_builder=MagicMock(spec=PromptBuilder), sandbox_pool=MagicMock(spec=SandboxPool),
                        vision_client=None, vision_model="")


def _session():
    now = datetime.now(timezone.utc)
    return Session(id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="writer", channel="web", status="active", config={},
                   created_at=now, updated_at=now)


def _acknowledging(monkeypatch, *, editing=True):
    """acknowledge_mention, recorded: thread "gone" is refused by the editor, thread "boom" raises."""
    calls: list[tuple[str, str]] = []

    async def acknowledge(mention, **kwargs):
        calls.append((mention["thread"], kwargs["session_id"]))
        if mention["thread"] == "boom":
            raise RuntimeError("runtime-office is down")
        return json.dumps({"error": "Comment thread gone is gone."} if mention["thread"] == "gone" else {"applied": True})

    monkeypatch.setattr(office_tools, "acknowledge_mention", acknowledge)
    monkeypatch.setattr(loop_module, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=editing)))
    return calls


def test_only_mentions_no_wake_or_request_has_followed_are_acknowledged():
    events = [_mention(1, thread="aaaaaaaa"), _event(2, EventType.HARNESS_WAKE), _event(3, EventType.LLM_REQUEST),
              _event(4, EventType.USER_MESSAGE, {"content": "hi"}), _mention(5)]
    assert unacknowledged(events) == [MENTION]
    assert unacknowledged([*events, _event(6, EventType.LLM_REQUEST)]) == []


def test_the_model_is_told_where_the_thread_is_and_to_answer_with_the_result_only():
    note = mention_note(_mention(1).data)
    assert '"15689582"' in note and "may be a reply" in note
    assert "only the result" in note and "Resolve the thread last" in note and "On it, I'll reply here when done." in note
    pdf = mention_note(_mention(1, kind="pdf", path="c.pdf").data)
    assert "names no thread" in pdf and "replies end with the acknowledgement" in pdf and "@agent.invalid" in pdf
    assert "15689582" not in pdf and "exactly" not in pdf
    assert mention_note({"content": "hi", "metadata": MENTION}) is None
    # Folded into the message as replay rebuilds it, so a later turn reads the same bytes.
    assert build_user_message_dict(_mention(1).data)["content"] == f"{note}\n\n{CONTENT}"


@pytest.mark.asyncio
async def test_each_new_mention_is_acknowledged_and_a_failure_never_stops_the_turn(monkeypatch):
    calls = _acknowledging(monkeypatch)
    harness, session = _harness(AsyncMock()), _session()
    await harness._acknowledge_mentions(session, [_mention(1, thread="boom"), _mention(2, thread="gone"), _mention(3)])
    assert calls == [("boom", str(session.id)), ("gone", str(session.id)), ("15689582", str(session.id))]
    assert harness._mention_paths == ("docs/Plan.docx",)


@pytest.mark.asyncio
async def test_nothing_is_acknowledged_where_the_agent_does_not_edit_live(monkeypatch):
    calls = _acknowledging(monkeypatch, editing=False)
    await _harness(AsyncMock())._acknowledge_mentions(_session(), [_mention(1)])
    assert calls == []


@pytest.mark.asyncio
async def test_a_mention_that_arrives_mid_turn_is_acknowledged_before_the_model_reads_it(monkeypatch):
    calls = _acknowledging(monkeypatch)
    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[_mention(9)])
    harness, session = _harness(store), _session()
    message, cursor = await harness._collect_steer_messages(session, 8)
    assert calls == [("15689582", str(session.id))] and cursor == 9
    assert "Resolve the thread last" in message["content"] and CONTENT in message["content"]


@pytest.mark.asyncio
async def test_the_turns_save_takes_the_acknowledged_documents_and_forgets_them(monkeypatch):
    saved: list = []

    async def save_turn(**kwargs):
        saved.append(kwargs["also"])

    monkeypatch.setattr(turn_end, "save_turn", save_turn)
    monkeypatch.setattr(completion_module, "load_settings", lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True)))
    harness = _harness(AsyncMock())
    harness._mention_paths = ("docs/Plan.docx",)
    await harness._save_office_documents(_session())
    assert saved == [("docs/Plan.docx",)] and harness._mention_paths == ()
