"""Tests for idle session reset with LLM-powered memory flush.

Covers:
- SessionResetSettings configuration
- SessionStore.find_idle_sessions() query logic
- SessionStore.reset_session() state clearing
- Transcript extraction from event log
- Flush prompt construction with stale-overwrite guard
- Flush agent mini-loop (mock LLM)
- Memory persistence to TenantStorage
- End-to-end flush_and_reset_session orchestration

Ported from Hermes test_session_reset_notify.py,
test_async_memory_flush.py, and test_flush_memory_stale_guard.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.config import SessionResetSettings
from surogates.memory.manager import MemoryManager
from surogates.memory.store import ENTRY_DELIMITER, MemoryStore
from surogates.session.events import EventType
from surogates.session.models import Event, Session


# =========================================================================
# Helpers
# =========================================================================


def _make_event(
    event_type: EventType,
    data: dict,
    event_id: int = 1,
    session_id: UUID | None = None,
) -> Event:
    return Event(
        id=event_id,
        session_id=session_id or uuid4(),
        type=event_type.value,
        data=data,
        created_at=datetime.now(tz=timezone.utc),
    )


def _make_session(
    updated_at: datetime | None = None,
    status: str = "active",
    session_id: UUID | None = None,
) -> Session:
    now = datetime.now(tz=timezone.utc)
    return Session(
        id=session_id or uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="test-agent",
        channel="web",
        status=status,
        config={},
        message_count=10,
        tool_call_count=5,
        input_tokens=5000,
        output_tokens=2000,
        created_at=now,
        updated_at=updated_at or now,
    )


# =========================================================================
# SessionResetSettings
# =========================================================================


class TestSessionResetSettings:
    """SessionResetSettings configuration validation."""

    def test_defaults(self):
        s = SessionResetSettings()
        assert s.enabled is False
        assert s.mode == "idle"
        assert s.idle_minutes == 1440
        assert s.at_hour == 4
        assert s.flush_max_iterations == 8
        assert s.flush_max_retries == 3
        assert s.watcher_interval_seconds == 300
        assert s.notify is True
        assert "webhook" in s.notify_exclude_channels

    def test_custom_values(self):
        s = SessionResetSettings(
            enabled=True,
            mode="both",
            idle_minutes=60,
            at_hour=6,
            flush_max_iterations=4,
            flush_max_retries=5,
            notify=False,
            notify_exclude_channels=["webhook", "telegram"],
        )
        assert s.enabled is True
        assert s.mode == "both"
        assert s.idle_minutes == 60
        assert s.at_hour == 6
        assert s.flush_max_iterations == 4
        assert s.flush_max_retries == 5
        assert s.notify is False
        assert "telegram" in s.notify_exclude_channels


# =========================================================================
# Transcript extraction
# =========================================================================


class TestExtractTranscript:
    """extract_transcript should build a clean user/assistant transcript."""

    def test_basic_conversation(self):
        from surogates.jobs.reset_idle_sessions import extract_transcript

        sid = uuid4()
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "hello"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "Hi there!"}},
                2, sid,
            ),
            _make_event(EventType.USER_MESSAGE, {"content": "my name is Alice"}, 3, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "Nice to meet you, Alice!"}},
                4, sid,
            ),
        ]

        transcript = extract_transcript(events)
        assert len(transcript) == 4
        assert transcript[0] == {"role": "user", "content": "hello"}
        assert transcript[1] == {"role": "assistant", "content": "Hi there!"}
        assert transcript[2] == {"role": "user", "content": "my name is Alice"}
        assert transcript[3] == {"role": "assistant", "content": "Nice to meet you, Alice!"}

    def test_skips_tool_events(self):
        from surogates.jobs.reset_idle_sessions import extract_transcript

        sid = uuid4()
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "search for cats"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": None, "tool_calls": []}},
                2, sid,
            ),
            _make_event(EventType.TOOL_CALL, {"name": "web_search", "args": {}}, 3, sid),
            _make_event(EventType.TOOL_RESULT, {"tool_call_id": "tc1", "content": "results"}, 4, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "Here are the results"}},
                5, sid,
            ),
        ]

        transcript = extract_transcript(events)
        assert len(transcript) == 2
        assert transcript[0]["role"] == "user"
        assert transcript[1]["content"] == "Here are the results"

    def test_content_blocks_format(self):
        """LLM responses with content blocks (list format) are handled."""
        from surogates.jobs.reset_idle_sessions import extract_transcript

        sid = uuid4()
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "explain"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": [
                    {"type": "text", "text": "First paragraph."},
                    {"type": "text", "text": "Second paragraph."},
                ]}},
                2, sid,
            ),
        ]

        transcript = extract_transcript(events)
        assert len(transcript) == 2
        assert "First paragraph." in transcript[1]["content"]
        assert "Second paragraph." in transcript[1]["content"]

    def test_context_compact_replaces_prior(self):
        """CONTEXT_COMPACT event replaces all prior messages."""
        from surogates.jobs.reset_idle_sessions import extract_transcript

        sid = uuid4()
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "old message"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "old response"}},
                2, sid,
            ),
            _make_event(
                EventType.CONTEXT_COMPACT,
                {"compacted_messages": [
                    {"role": "user", "content": "compacted user msg"},
                    {"role": "assistant", "content": "compacted assistant msg"},
                ]},
                3, sid,
            ),
            _make_event(EventType.USER_MESSAGE, {"content": "new message"}, 4, sid),
        ]

        transcript = extract_transcript(events)
        assert len(transcript) == 3
        assert transcript[0]["content"] == "compacted user msg"
        assert transcript[1]["content"] == "compacted assistant msg"
        assert transcript[2]["content"] == "new message"

    def test_empty_events(self):
        from surogates.jobs.reset_idle_sessions import extract_transcript
        assert extract_transcript([]) == []

    def test_skips_empty_content(self):
        """Events with empty or None content are skipped."""
        from surogates.jobs.reset_idle_sessions import extract_transcript

        sid = uuid4()
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": ""}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": ""}},
                2, sid,
            ),
            _make_event(EventType.USER_MESSAGE, {"content": "real message"}, 3, sid),
        ]

        transcript = extract_transcript(events)
        assert len(transcript) == 1
        assert transcript[0]["content"] == "real message"


# =========================================================================
# Flush prompt construction (stale-overwrite guard)
# =========================================================================


class TestBuildFlushPrompt:
    """build_flush_prompt should include memory guard when files exist."""

    def test_core_instructions_present(self, tmp_path: Path):
        from surogates.jobs.reset_idle_sessions import build_flush_prompt

        prompt = build_flush_prompt(tmp_path)
        assert "automatically reset" in prompt
        assert "Save any important facts" in prompt
        assert "consider saving it as a skill" in prompt
        assert "Do NOT respond to the user" in prompt

    def test_memory_content_injected(self, tmp_path: Path):
        """When memory files exist, their content appears in the prompt."""
        from surogates.jobs.reset_idle_sessions import build_flush_prompt

        (tmp_path / "MEMORY.md").write_text(
            f"Agent knows Python{ENTRY_DELIMITER}User prefers dark mode",
            encoding="utf-8",
        )
        (tmp_path / "USER.md").write_text(
            f"Name: Alice{ENTRY_DELIMITER}Timezone: PST",
            encoding="utf-8",
        )

        prompt = build_flush_prompt(tmp_path)

        assert "Agent knows Python" in prompt
        assert "User prefers dark mode" in prompt
        assert "Name: Alice" in prompt
        assert "Timezone: PST" in prompt
        assert "Do NOT overwrite or remove entries" in prompt
        assert "current live state of memory" in prompt

    def test_no_guard_without_memory_files(self, tmp_path: Path):
        """When no memory files exist, the guard section is absent."""
        from surogates.jobs.reset_idle_sessions import build_flush_prompt

        prompt = build_flush_prompt(tmp_path)
        assert "Do NOT overwrite or remove entries" not in prompt
        assert "current live state of memory" not in prompt

    def test_empty_memory_files_no_guard(self, tmp_path: Path):
        """Empty memory files should not trigger the guard section."""
        from surogates.jobs.reset_idle_sessions import build_flush_prompt

        (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")
        (tmp_path / "USER.md").write_text("  \n  ", encoding="utf-8")

        prompt = build_flush_prompt(tmp_path)
        assert "current live state of memory" not in prompt


# =========================================================================
# Flush agent mini-loop
# =========================================================================


def _mock_llm_response(content=None, tool_calls=None):
    """Build a mock OpenAI ChatCompletion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_tool_call(tc_id, name, arguments):
    """Build a mock tool call object."""
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    return tc


class TestRunFlushAgent:
    """run_flush_agent should call the LLM and process memory tool calls."""

    @pytest.mark.asyncio
    async def test_no_tool_calls_exits_immediately(self, tmp_path: Path):
        """Agent stops after first response if no tool calls."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        memory_store = MemoryStore(memory_dir=tmp_path / "mem")
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(
            return_value=_mock_llm_response(content="Nothing worth saving."),
        )

        await run_flush_agent(
            transcript=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
            max_iterations=8,
        )

        llm_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_tool_calls_processed(self, tmp_path: Path):
        """Agent calls memory tool and the store is updated."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        mem_dir = tmp_path / "mem"
        memory_store = MemoryStore(memory_dir=mem_dir)
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        # First response: tool call to add memory
        tool_call = _mock_tool_call(
            "tc_1", "memory",
            {"action": "add", "target": "user", "content": "User is Alice, a data scientist"},
        )
        response_with_tool = _mock_llm_response(tool_calls=[tool_call])

        # Second response: no tool calls (done)
        response_done = _mock_llm_response(content="Done saving.")

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(
            side_effect=[response_with_tool, response_done],
        )

        await run_flush_agent(
            transcript=[
                {"role": "user", "content": "I'm Alice, a data scientist"},
                {"role": "assistant", "content": "Nice to meet you!"},
                {"role": "user", "content": "I prefer dark mode"},
                {"role": "assistant", "content": "Noted!"},
            ],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
        )

        assert llm_client.chat.completions.create.call_count == 2
        assert len(memory_store.user_entries) == 1
        assert "Alice" in memory_store.user_entries[0]

    @pytest.mark.asyncio
    async def test_max_iterations_respected(self, tmp_path: Path):
        """Agent stops after max_iterations even if still calling tools."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        memory_store = MemoryStore(memory_dir=tmp_path / "mem")
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        # Always return a tool call.
        tool_call = _mock_tool_call(
            "tc_loop", "memory",
            {"action": "add", "target": "memory", "content": "loop entry"},
        )
        response_loop = _mock_llm_response(tool_calls=[tool_call])

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(return_value=response_loop)

        await run_flush_agent(
            transcript=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
            max_iterations=3,
        )

        assert llm_client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_llm_failure_handled_gracefully(self, tmp_path: Path):
        """LLM call failure doesn't crash the flush agent."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        memory_store = MemoryStore(memory_dir=tmp_path / "mem")
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(
            side_effect=Exception("API error"),
        )

        # Should not raise.
        await run_flush_agent(
            transcript=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
        )

    @pytest.mark.asyncio
    async def test_unknown_tool_rejected(self, tmp_path: Path):
        """Calls to unknown tools return an error, not crash."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        memory_store = MemoryStore(memory_dir=tmp_path / "mem")
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        # Tool call to a non-existent tool.
        tool_call = _mock_tool_call("tc_bad", "web_search", {"query": "cats"})
        response_bad = _mock_llm_response(tool_calls=[tool_call])
        response_done = _mock_llm_response(content="Ok done.")

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(
            side_effect=[response_bad, response_done],
        )

        await run_flush_agent(
            transcript=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
        )

        # Should complete without error.
        assert llm_client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_response(self, tmp_path: Path):
        """Multiple tool calls in a single response are all processed."""
        from surogates.jobs.reset_idle_sessions import run_flush_agent

        mem_dir = tmp_path / "mem"
        memory_store = MemoryStore(memory_dir=mem_dir)
        memory_manager = MemoryManager(memory_store)
        memory_manager.initialize_all()

        tc1 = _mock_tool_call(
            "tc_1", "memory",
            {"action": "add", "target": "user", "content": "User prefers Python"},
        )
        tc2 = _mock_tool_call(
            "tc_2", "memory",
            {"action": "add", "target": "memory", "content": "Project uses FastAPI"},
        )
        response_multi = _mock_llm_response(tool_calls=[tc1, tc2])
        response_done = _mock_llm_response(content="Done.")

        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(
            side_effect=[response_multi, response_done],
        )

        await run_flush_agent(
            transcript=[
                {"role": "user", "content": "I love Python and FastAPI"},
                {"role": "assistant", "content": "Great choices!"},
                {"role": "user", "content": "thanks"},
                {"role": "assistant", "content": "You're welcome!"},
            ],
            flush_prompt="[flush]",
            memory_manager=memory_manager,
            llm_client=llm_client,
            model="test-model",
        )

        assert len(memory_store.user_entries) == 1
        assert "Python" in memory_store.user_entries[0]
        assert len(memory_store.memory_entries) == 1
        assert "FastAPI" in memory_store.memory_entries[0]


# =========================================================================
# Memory persistence to TenantStorage
# =========================================================================


class TestPersistMemoryToStorage:
    """persist_memory_to_storage copies files to S3/Garage."""

    @pytest.mark.asyncio
    async def test_persists_both_files(self, tmp_path: Path):
        from surogates.jobs.reset_idle_sessions import persist_memory_to_storage

        mem_dir = tmp_path / "mem"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("fact one", encoding="utf-8")
        (mem_dir / "USER.md").write_text("user info", encoding="utf-8")

        storage = AsyncMock()
        storage.bucket_exists = AsyncMock(return_value=True)

        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")

        await persist_memory_to_storage(
            memory_dir=mem_dir,
            storage=storage,
            org_id=org_id,
            user_id=user_id,
        )

        assert storage.write_text.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_missing_files(self, tmp_path: Path):
        from surogates.jobs.reset_idle_sessions import persist_memory_to_storage

        mem_dir = tmp_path / "no_files"
        mem_dir.mkdir()

        storage = AsyncMock()
        storage.bucket_exists = AsyncMock(return_value=True)

        org_id = UUID("00000000-0000-0000-0000-000000000001")
        user_id = UUID("00000000-0000-0000-0000-000000000002")

        await persist_memory_to_storage(
            memory_dir=mem_dir,
            storage=storage,
            org_id=org_id,
            user_id=user_id,
        )

        storage.write_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_files(self, tmp_path: Path):
        from surogates.jobs.reset_idle_sessions import persist_memory_to_storage

        mem_dir = tmp_path / "empty"
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("", encoding="utf-8")
        (mem_dir / "USER.md").write_text("   \n  ", encoding="utf-8")

        storage = AsyncMock()
        storage.bucket_exists = AsyncMock(return_value=True)

        await persist_memory_to_storage(
            memory_dir=mem_dir,
            storage=storage,
            org_id=UUID("00000000-0000-0000-0000-000000000001"),
            user_id=UUID("00000000-0000-0000-0000-000000000002"),
        )

        storage.write_text.assert_not_called()


# =========================================================================
# End-to-end flush_and_reset_session
# =========================================================================


class TestFlushAndResetSession:
    """End-to-end orchestration of flush + reset."""

    @pytest.mark.asyncio
    async def test_short_transcript_skips_flush(self, tmp_path: Path):
        """Sessions with < 4 transcript messages skip the LLM flush."""
        from surogates.jobs.reset_idle_sessions import flush_and_reset_session

        session = _make_session()
        sid = session.id

        # Only 2 events -> 2 transcript messages (below threshold).
        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "hi"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "hello"}},
                2, sid,
            ),
        ]

        session_store = AsyncMock()
        session_store.get_events = AsyncMock(return_value=events)
        session_store.reset_session = AsyncMock()
        session_store.emit_event = AsyncMock(return_value=1)

        settings = MagicMock()
        settings.session_reset = SessionResetSettings(enabled=True)
        settings.tenant_assets_root = str(tmp_path)
        settings.llm.model = "test-model"
        settings.redis.url = "redis://localhost:6379/0"

        llm_client = AsyncMock()
        storage = AsyncMock()

        result = await flush_and_reset_session(
            session=session,
            session_store=session_store,
            settings=settings,
            llm_client=llm_client,
            storage=storage,
            redis_client=AsyncMock(),
        )

        assert result is True
        session_store.reset_session.assert_called_once()
        # LLM should NOT have been called (transcript too short).
        llm_client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_events_resets_immediately(self):
        """Sessions with no events are reset without flushing."""
        from surogates.jobs.reset_idle_sessions import flush_and_reset_session

        session = _make_session()

        session_store = AsyncMock()
        session_store.get_events = AsyncMock(return_value=[])
        session_store.reset_session = AsyncMock()
        session_store.emit_event = AsyncMock(return_value=1)

        settings = MagicMock()
        settings.session_reset = SessionResetSettings(enabled=True)

        result = await flush_and_reset_session(
            session=session,
            session_store=session_store,
            settings=settings,
            llm_client=AsyncMock(),
            storage=AsyncMock(),
            redis_client=AsyncMock(),
        )

        assert result is True
        session_store.reset_session.assert_called_once_with(
            session.id, reason="idle_no_events",
        )

    @pytest.mark.asyncio
    async def test_full_flush_and_reset(self, tmp_path: Path):
        """Full flow: extract transcript, run flush agent, reset."""
        from surogates.jobs.reset_idle_sessions import flush_and_reset_session

        session = _make_session()
        sid = session.id
        org_id = session.org_id

        events = [
            _make_event(EventType.USER_MESSAGE, {"content": "I'm a data scientist"}, 1, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "Great, how can I help?"}},
                2, sid,
            ),
            _make_event(EventType.USER_MESSAGE, {"content": "I prefer pandas over polars"}, 3, sid),
            _make_event(
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": "Noted, pandas it is."}},
                4, sid,
            ),
        ]

        session_store = AsyncMock()
        session_store.get_events = AsyncMock(return_value=events)
        session_store.reset_session = AsyncMock()
        session_store.emit_event = AsyncMock(return_value=1)

        mem_dir = tmp_path / str(org_id) / "users" / str(session.user_id) / "memory"
        mem_dir.mkdir(parents=True)

        settings = MagicMock()
        settings.session_reset = SessionResetSettings(enabled=True)
        settings.tenant_assets_root = str(tmp_path)
        settings.llm.model = "test-model"

        tool_call = _mock_tool_call(
            "tc_1", "memory",
            {"action": "add", "target": "user", "content": "Data scientist, prefers pandas"},
        )
        llm_client = AsyncMock()
        llm_client.chat.completions.create = AsyncMock(side_effect=[
            _mock_llm_response(tool_calls=[tool_call]),
            _mock_llm_response(content="Done."),
        ])

        storage = AsyncMock()
        storage.bucket_exists = AsyncMock(return_value=True)
        mock_redis = AsyncMock()

        result = await flush_and_reset_session(
            session=session,
            session_store=session_store,
            settings=settings,
            llm_client=llm_client,
            storage=storage,
            redis_client=mock_redis,
        )

        assert result is True
        assert llm_client.chat.completions.create.call_count == 2

        mem_file = mem_dir / "USER.md"
        assert mem_file.exists()
        assert "pandas" in mem_file.read_text(encoding="utf-8")

        session_store.reset_session.assert_called_once_with(sid, reason="idle")
        mock_redis.publish.assert_called_once()


# =========================================================================
# SESSION_RESET event type
# =========================================================================


class TestSessionResetEventType:
    """SESSION_RESET event type exists and has correct value."""

    def test_event_type_exists(self):
        assert hasattr(EventType, "SESSION_RESET")
        assert EventType.SESSION_RESET.value == "session.reset"

    def test_event_type_is_unique(self):
        values = [e.value for e in EventType]
        assert values.count("session.reset") == 1
