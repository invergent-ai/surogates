"""Which sessions' models may make, change and read schedules.

A user's or a service account's own top-level chat makes and changes them,
whatever ``/loop`` and ``ask_user_question`` are set to: only the policy,
by tool name, takes one away.  Threads and helpers of an owned session
read them; a scheduled run reads them and pauses, changes or ends its own.  An anonymous visitor and an evaluation row get
none.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.channels.memory_boundary import EVAL_BOUNDARY_PREFIX
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.session.models import Session
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _final_response, _make_loop_harness

WRITE_TOOLS = {"cron_create", "cron_update", "cron_pause", "cron_resume", "cron_run_now", "cron_delete"}
READ_TOOLS = {"cron_list", "cron_get"}
ROUTINE_TOOLS = WRITE_TOOLS | READ_TOOLS


def _session(*, user: bool = True, service_account: bool = False, parent: bool = False, **config) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        user_id=uuid4() if user else None,
        service_account_id=uuid4() if service_account else None,
        parent_id=uuid4() if parent else None,
        config=config, created_at=now, updated_at=now,
    )


async def _offered(monkeypatch, session: Session, *, loop: bool = True, denied: frozenset = frozenset()) -> set[str]:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools = registry
    if denied:
        harness._governance_gate = SimpleNamespace(
            offers=lambda name: name not in denied, allow_listed=lambda names: frozenset(),
        )
    harness._slash_commands = SlashCommandConfig() if loop else SlashCommandConfig(
        commands=SLASH_COMMAND_IDS - {"loop"},
    )
    sent: list[set[str]] = []

    async def llm(**kwargs):
        sent.append({schema["function"]["name"] for schema in kwargs["create_kwargs"]["tools"]})
        return _final_response("Done.")

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    await harness._run_loop(
        session, [{"role": "user", "content": "every weekday at 8:30"}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=[],
    )
    return sent[0]


@pytest.mark.parametrize("session", [
    _session(),
    _session(user=False, service_account=True),
], ids=["a-users-chat", "a-service-accounts-chat"])
@pytest.mark.asyncio
async def test_an_ordinary_chat_is_offered_the_cron_tools(monkeypatch, session):
    assert ROUTINE_TOOLS <= await _offered(monkeypatch, session)


@pytest.mark.parametrize("session", [
    _session(user=False),
    _session(memory_boundary=f"{EVAL_BOUNDARY_PREFIX}run-1"),
], ids=["anonymous", "an-eval-row"])
@pytest.mark.asyncio
async def test_a_session_no_one_owns_or_answers_gets_none(monkeypatch, session):
    assert not ROUTINE_TOOLS & await _offered(monkeypatch, session)


@pytest.mark.parametrize("session", [
    _session(parent=True, workstream_role="thread"),
    _session(parent=True),
], ids=["a-project-thread", "a-worker"])
@pytest.mark.asyncio
async def test_helpers_read_schedules_and_make_none(monkeypatch, session):
    offered = await _offered(monkeypatch, session)
    assert READ_TOOLS <= offered
    assert not WRITE_TOOLS & offered


OWN_SCHEDULE_TOOLS = {"cron_update", "cron_pause", "cron_delete"}


@pytest.mark.parametrize("session", [
    _session(parent=True, scheduled_session_id=str(uuid4())),
    _session(scheduled_session_id=str(uuid4())),
], ids=["a-scheduled-run", "a-detached-scheduled-run"])
@pytest.mark.asyncio
async def test_a_run_reads_schedules_and_may_change_only_its_own(monkeypatch, session):
    offered = await _offered(monkeypatch, session)
    assert READ_TOOLS | OWN_SCHEDULE_TOOLS <= offered
    assert not {"cron_create", "cron_resume", "cron_run_now"} & offered


@pytest.mark.asyncio
async def test_the_policy_still_takes_a_runs_tools_away(monkeypatch):
    run = _session(parent=True, scheduled_session_id=str(uuid4()))
    offered = await _offered(monkeypatch, run, denied=frozenset({"cron_delete"}))
    assert "cron_delete" not in offered
    assert {"cron_pause", "cron_update"} <= offered


@pytest.mark.asyncio
async def test_neither_loop_off_nor_a_denied_question_tool_takes_them_away(monkeypatch):
    assert ROUTINE_TOOLS <= await _offered(monkeypatch, _session(), loop=False)
    offered = await _offered(monkeypatch, _session(), denied=frozenset({"ask_user_question"}))
    assert ROUTINE_TOOLS <= offered and "ask_user_question" not in offered


@pytest.mark.asyncio
async def test_the_policy_takes_away_only_what_it_names(monkeypatch):
    offered = await _offered(monkeypatch, _session(), denied=frozenset({"cron_create", "cron_delete"}))
    assert not {"cron_create", "cron_delete"} & offered
    assert READ_TOOLS <= offered
