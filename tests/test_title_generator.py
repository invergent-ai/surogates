"""Tests for automatic session title generation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.title_generator import (
    clean_generated_title,
    generate_session_title,
    maybe_generate_session_title,
)
from surogates.harness.loop import AgentHarness
from surogates.session.models import Event, Session, SessionLease


def _response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ]
    )


def test_clean_generated_title_removes_wrapping_noise() -> None:
    assert clean_generated_title('"Title: Build a Billing Dashboard."') == (
        "Build a Billing Dashboard"
    )


def test_clean_generated_title_limits_length() -> None:
    raw = "x" * 100

    title = clean_generated_title(raw)

    assert len(title) == 80
    assert title.endswith("...")


@pytest.mark.asyncio
async def test_generate_session_title_uses_auxiliary_chat_client() -> None:
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=_response("Title: Debug Redis Failures."))
            )
        )
    )

    title = await generate_session_title(
        llm_client=llm_client,
        model="gpt-4o-mini",
        user_message="Redis is timing out in production",
    )

    assert title == "Debug Redis Failures"
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["stream"] is False
    assert call_kwargs["max_tokens"] <= 32
    assert "extra_body" not in call_kwargs
    assert "Redis is timing out" in call_kwargs["messages"][1]["content"]


@pytest.mark.asyncio
async def test_generate_session_title_disables_thinking_for_surogate_model() -> None:
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=_response("Bitcoin Price Loop"))
            )
        )
    )

    title = await generate_session_title(
        llm_client=llm_client,
        model="surogate",
        user_message="watch the bitcoin price",
    )

    assert title == "Bitcoin Price Loop"
    call_kwargs = llm_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.asyncio
async def test_generate_session_title_retries_without_optional_params() -> None:
    create = AsyncMock(
        side_effect=[
            TypeError("unexpected keyword argument 'max_tokens'"),
            _response("Debug Redis Failures"),
        ]
    )
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )

    title = await generate_session_title(
        llm_client=llm_client,
        model="gpt-5",
        user_message="Redis is timing out in production",
    )

    assert title == "Debug Redis Failures"
    first_call = create.await_args_list[0].kwargs
    retry_call = create.await_args_list[1].kwargs
    assert first_call["max_tokens"] <= 32
    assert "max_tokens" not in retry_call
    assert "temperature" not in retry_call


@pytest.mark.asyncio
async def test_generate_session_title_retry_removes_thinking_extra_body() -> None:
    create = AsyncMock(
        side_effect=[
            RuntimeError("unsupported parameter: chat_template_kwargs"),
            _response("Bitcoin Price Loop"),
        ]
    )
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )
    )

    title = await generate_session_title(
        llm_client=llm_client,
        model="surogate",
        user_message="watch the bitcoin price",
    )

    assert title == "Bitcoin Price Loop"
    first_call = create.await_args_list[0].kwargs
    retry_call = create.await_args_list[1].kwargs
    assert "extra_body" in first_call
    assert "extra_body" not in retry_call


@pytest.mark.asyncio
async def test_generate_session_title_returns_none_on_llm_failure() -> None:
    llm_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("provider down"))
            )
        )
    )

    title = await generate_session_title(
        llm_client=llm_client,
        model="gpt-4o-mini",
        user_message="hello",
    )

    assert title is None


@pytest.mark.asyncio
async def test_maybe_generate_session_title_returns_none_when_no_user_message() -> None:
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock())
    llm_client = SimpleNamespace()
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")

    title = await maybe_generate_session_title(
        store=store,
        llm_client=llm_client,
        session=session,
        messages=[{"role": "assistant", "content": "Hi"}],
        model="gpt-4o",
    )

    assert title is None
    store.update_session_title_if_empty.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_generate_session_title_sets_title_for_first_message(monkeypatch) -> None:
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock(return_value=True))
    llm_client = SimpleNamespace()
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")

    async def fake_generate_session_title(**_kwargs):
        return "Build Sales Chart"

    monkeypatch.setattr(
        "surogates.harness.title_generator.generate_session_title",
        fake_generate_session_title,
    )

    title = await maybe_generate_session_title(
        store=store,
        llm_client=llm_client,
        session=session,
        messages=[{"role": "user", "content": "build a chart"}],
        model="gpt-4o",
    )

    assert title == "Build Sales Chart"
    store.update_session_title_if_empty.assert_awaited_once_with(
        session.id,
        "Build Sales Chart",
    )


@pytest.mark.asyncio
async def test_maybe_generate_session_title_uses_per_agent_summary_slot() -> None:
    """The title runs against the agent's resolved ``llm_summary`` slot.

    Regression cover for the shared-runtime bug where the title path
    rebuilt a client from the static global ``Settings.llm.summary_*``
    (whose ``summary_base_url`` pointed at a proxy ``/v1`` route that does
    not exist per-agent) and 404'd on every call.  When a summary slot is
    supplied it must be used verbatim — never the global config.
    """
    summary_create = AsyncMock(return_value=_response("Summary Slot Title"))
    summary_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=summary_create)
        )
    )
    main_create = AsyncMock(return_value=_response("Main Model Title"))
    main_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=main_create)
        )
    )
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock(return_value=True))
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")

    title = await maybe_generate_session_title(
        store=store,
        llm_client=main_client,
        session=session,
        messages=[{"role": "user", "content": "summarize the report"}],
        model="gpt-4o",
        summary_client=summary_client,
        summary_model="agent-summary-model",
    )

    assert title == "Summary Slot Title"
    summary_create.assert_awaited_once()
    main_create.assert_not_called()
    assert summary_create.await_args.kwargs["model"] == "agent-summary-model"


@pytest.mark.asyncio
async def test_maybe_generate_session_title_falls_back_to_main_without_slot() -> None:
    """No summary slot → the title runs against the main turn client.

    The agent's main endpoint is per-agent-correct, so this is a safe
    fallback (and still avoids the dead global summary endpoint).
    """
    main_create = AsyncMock(return_value=_response("Main Model Title"))
    main_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=main_create)
        )
    )
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock(return_value=True))
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")

    title = await maybe_generate_session_title(
        store=store,
        llm_client=main_client,
        session=session,
        messages=[{"role": "user", "content": "summarize the report"}],
        model="gpt-4o",
    )

    assert title == "Main Model Title"
    main_create.assert_awaited_once()
    assert main_create.await_args.kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_maybe_generate_session_title_skips_later_exchanges(monkeypatch) -> None:
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock())
    llm_client = SimpleNamespace()
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")
    generate = AsyncMock(return_value="Too Late")
    monkeypatch.setattr(
        "surogates.harness.title_generator.generate_session_title",
        generate,
    )

    title = await maybe_generate_session_title(
        store=store,
        llm_client=llm_client,
        session=session,
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "third"},
        ],
        model="gpt-4o",
    )

    assert title is None
    generate.assert_not_called()
    store.update_session_title_if_empty.assert_not_called()


@pytest.mark.asyncio
async def test_harness_title_hook_runs_in_background(monkeypatch) -> None:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = SimpleNamespace(emit_event=AsyncMock(return_value=42))
    harness._llm = SimpleNamespace()
    harness._tenant = SimpleNamespace()
    harness._summary_client = SimpleNamespace()
    harness._summary_model = "agent-summary-model"
    harness._background_tasks = set()

    maybe_generate = AsyncMock(return_value="Build Sales Chart")
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )
    session = SimpleNamespace(id=uuid4(), title=None)
    messages = [{"role": "user", "content": "build a chart"}]

    harness._maybe_generate_title(
        session=session,
        messages=messages,
        model="gpt-4o",
    )

    # The call should not have happened synchronously -- it was scheduled.
    maybe_generate.assert_not_called()
    assert len(harness._background_tasks) == 1

    # Drain the background task and verify the underlying generator ran.
    await asyncio.gather(*list(harness._background_tasks))

    maybe_generate.assert_awaited_once_with(
        store=harness._store,
        llm_client=harness._llm,
        session=session,
        messages=messages,
        model="gpt-4o",
        summary_client=harness._summary_client,
        summary_model=harness._summary_model,
    )
    # A successful title write must emit SESSION_TITLE_UPDATED so the SSE
    # stream can patch the sidebar without an explicit refetch.
    from surogates.session.events import EventType

    harness._store.emit_event.assert_awaited_once_with(
        session.id,
        EventType.SESSION_TITLE_UPDATED,
        {"title": "Build Sales Chart"},
    )
    assert harness._background_tasks == set()


@pytest.mark.asyncio
async def test_harness_title_hook_skips_event_when_no_title(monkeypatch) -> None:
    """No title generated → no SESSION_TITLE_UPDATED event."""
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = SimpleNamespace(emit_event=AsyncMock())
    harness._llm = SimpleNamespace()
    harness._tenant = SimpleNamespace()
    harness._summary_client = None
    harness._summary_model = ""
    harness._background_tasks = set()

    maybe_generate = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )
    session = SimpleNamespace(id=uuid4(), title=None)
    messages = [{"role": "user", "content": "build a chart"}]

    harness._maybe_generate_title(
        session=session,
        messages=messages,
        model="gpt-4o",
    )
    await asyncio.gather(*list(harness._background_tasks))

    maybe_generate.assert_awaited_once()
    harness._store.emit_event.assert_not_called()


@pytest.mark.asyncio
async def test_harness_title_hook_skips_when_title_already_set(monkeypatch) -> None:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = SimpleNamespace()
    harness._llm = SimpleNamespace()
    harness._tenant = SimpleNamespace()
    harness._background_tasks = set()

    maybe_generate = AsyncMock(return_value="Whatever")
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )
    session = SimpleNamespace(id=uuid4(), title="Existing")
    messages = [{"role": "user", "content": "build a chart"}]

    harness._maybe_generate_title(
        session=session,
        messages=messages,
        model="gpt-4o",
    )

    assert harness._background_tasks == set()
    maybe_generate.assert_not_called()


@pytest.mark.asyncio
async def test_wake_kicks_off_title_for_loop_command(monkeypatch) -> None:
    """``wake()`` schedules title generation for /loop commands.

    Regression cover for what ``test_loop_command_response_generates_title``
    used to assert on the now-removed inline hook in ``_emit_loop_response``.
    Title generation is owned by the wake-time trigger; /loop sessions must
    still get titled even though the command short-circuits before the LLM
    loop runs.
    """
    session_id = uuid4()
    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-1",
        channel="web",
        status="active",
        config={},
        title=None,
        model="gpt-4o",
        created_at=now,
        updated_at=now,
    )
    lease = SessionLease(
        session_id=session_id,
        owner_id="test-worker",
        lease_token=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    user_event = Event(
        id=1,
        session_id=session_id,
        type="user.message",
        data={"content": "/loop check bitcoin volatility"},
        created_at=now,
    )

    rebuilt_messages = [
        {"role": "user", "content": "/loop check bitcoin volatility"},
    ]

    harness = AgentHarness.__new__(AgentHarness)
    harness._llm = AsyncMock()
    harness._worker_id = "test-worker"
    harness._default_model = "gpt-4o"
    harness._streaming_enabled = True
    harness._memory_manager = None
    harness._tenant = SimpleNamespace(org_id=session.org_id, user_id=session.user_id)
    harness._summary_client = None
    harness._summary_model = ""
    harness._session_factory = None
    harness._bundle = None
    harness._prompt = MagicMock()
    harness._background_tasks = set()

    store = AsyncMock()
    store.get_session = AsyncMock(return_value=session)
    store.try_acquire_lease = AsyncMock(return_value=lease)
    store.get_harness_cursor = AsyncMock(return_value=0)
    store.get_events = AsyncMock(return_value=[user_event])
    store.emit_event = AsyncMock(return_value=2)
    store.release_lease = AsyncMock()
    store.renew_lease = AsyncMock()
    harness._store = store

    harness._renew_lease_forever = AsyncMock()
    harness._rebuild_messages = MagicMock(return_value=rebuilt_messages)
    harness._engineer_context = AsyncMock(side_effect=lambda _s, _e, m: m)
    harness._build_system_prompt = AsyncMock(return_value="")
    harness._handle_loop_command = AsyncMock()

    monkeypatch.setattr("surogates.trace.new_span", lambda: None)
    monkeypatch.setattr(
        "surogates.harness.loop.cleanup_dead_connections",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "surogates.harness.loop.resolve_agent_def",
        AsyncMock(return_value=None),
    )

    maybe_generate = AsyncMock(return_value="Track Bitcoin Volatility")
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )

    await harness.wake(session_id)

    # The /loop command short-circuit fired, not the LLM loop.
    harness._handle_loop_command.assert_awaited_once()

    # Title hook ran with the /loop user message and was drained on shutdown.
    maybe_generate.assert_awaited_once()
    call_kwargs = maybe_generate.await_args.kwargs
    assert call_kwargs["messages"] == rebuilt_messages
    assert call_kwargs["model"] == "gpt-4o"
    assert harness._background_tasks == set()
