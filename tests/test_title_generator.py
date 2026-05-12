"""Tests for automatic session title generation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.title_generator import (
    clean_generated_title,
    generate_session_title,
    maybe_generate_session_title,
)
from surogates.harness.loop import AgentHarness


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
        assistant_response="Let's inspect connection pool metrics.",
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
        assistant_response="Loop scheduled.",
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
        assistant_response="Let's inspect connection pool metrics.",
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
        assistant_response="Loop scheduled.",
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
        assistant_response="hi",
    )

    assert title is None


@pytest.mark.asyncio
async def test_maybe_generate_session_title_skips_existing_title() -> None:
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock())
    llm_client = SimpleNamespace()
    session = SimpleNamespace(id=uuid4(), title="Existing", model="gpt-4o")

    title = await maybe_generate_session_title(
        store=store,
        llm_client=llm_client,
        session=session,
        messages=[{"role": "user", "content": "build a chart"}],
        assistant_message={"role": "assistant", "content": "Done."},
        model="gpt-4o",
    )

    assert title is None
    store.update_session_title_if_empty.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_generate_session_title_sets_title_for_first_exchange(monkeypatch) -> None:
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
        assistant_message={"role": "assistant", "content": "Done."},
        model="gpt-4o",
    )

    assert title == "Build Sales Chart"
    store.update_session_title_if_empty.assert_awaited_once_with(
        session.id,
        "Build Sales Chart",
    )


@pytest.mark.asyncio
async def test_maybe_generate_session_title_uses_summary_model_from_config(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "missing-config.yaml"))
    monkeypatch.setenv("SUROGATES_LLM_SUMMARY_MODEL", "summary-title-model")
    aux_create = AsyncMock(return_value=_response("Summary Model Title"))
    aux_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=aux_create)
        )
    )
    main_create = AsyncMock(return_value=_response("Main Model Title"))
    main_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=main_create)
        )
    )
    monkeypatch.setattr(
        "surogates.harness.auxiliary_client.AsyncOpenAI",
        lambda **_kwargs: aux_client,
    )
    store = SimpleNamespace(update_session_title_if_empty=AsyncMock(return_value=True))
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")

    title = await maybe_generate_session_title(
        store=store,
        llm_client=main_client,
        session=session,
        messages=[{"role": "user", "content": "summarize the report"}],
        assistant_message={"role": "assistant", "content": "Report summarized."},
        model="gpt-4o",
    )

    assert title == "Summary Model Title"
    aux_create.assert_awaited_once()
    main_create.assert_not_called()
    assert aux_create.await_args.kwargs["model"] == "summary-title-model"


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
        assistant_message={"role": "assistant", "content": "Done."},
        model="gpt-4o",
    )

    assert title is None
    generate.assert_not_called()
    store.update_session_title_if_empty.assert_not_called()


@pytest.mark.asyncio
async def test_harness_title_hook_delegates_best_effort(monkeypatch) -> None:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = SimpleNamespace()
    harness._llm = SimpleNamespace()
    harness._tenant = SimpleNamespace()

    maybe_generate = AsyncMock(return_value="Build Sales Chart")
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )
    session = SimpleNamespace(id=uuid4(), title=None)
    assistant_message = {"role": "assistant", "content": "Done."}
    messages = [{"role": "user", "content": "build a chart"}, assistant_message]

    await harness._maybe_generate_title(
        session=session,
        messages=messages,
        assistant_message=assistant_message,
        model="gpt-4o",
    )

    maybe_generate.assert_awaited_once_with(
        store=harness._store,
        llm_client=harness._llm,
        session=session,
        messages=messages,
        assistant_message=assistant_message,
        model="gpt-4o",
        tenant=harness._tenant,
    )


@pytest.mark.asyncio
async def test_loop_command_response_generates_title(monkeypatch) -> None:
    harness = AgentHarness.__new__(AgentHarness)
    harness._store = SimpleNamespace(
        emit_event=AsyncMock(return_value=42),
        advance_harness_cursor=AsyncMock(),
    )
    harness._llm = SimpleNamespace()
    harness._tenant = SimpleNamespace()
    harness._current_model = None
    harness._default_model = "gpt-4o"
    maybe_generate = AsyncMock(return_value="Track Bitcoin Volatility")
    monkeypatch.setattr(
        "surogates.harness.loop.maybe_generate_session_title",
        maybe_generate,
    )
    session = SimpleNamespace(id=uuid4(), title=None, model="gpt-4o")
    lease = SimpleNamespace(lease_token=uuid4())

    await harness._emit_loop_response(
        session,
        lease,
        "Loop scheduled.",
        user_content="/loop check bitcoin volatility",
    )

    maybe_generate.assert_awaited_once_with(
        store=harness._store,
        llm_client=harness._llm,
        session=session,
        messages=[
            {"role": "user", "content": "/loop check bitcoin volatility"},
            {"role": "assistant", "content": "Loop scheduled."},
        ],
        assistant_message={"role": "assistant", "content": "Loop scheduled."},
        model="gpt-4o",
        tenant=harness._tenant,
    )
