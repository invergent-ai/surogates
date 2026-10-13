"""Which sessions' models may make a schedule with ``cron_create``.

Every chat whose sender owns what it makes and can be asked first: a user's
or a service account's own top-level session that is offered
``ask_user_question``, with ``/loop`` on.  Never an anonymous visitor, a
scheduled run, a project's thread or anything else working under a parent,
nor a session no one answers.
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

ROUTINE_TOOLS = {"cron_create", "cron_list", "cron_delete"}


def _session(*, user: bool = True, service_account: bool = False, parent: bool = False, **config) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        user_id=uuid4() if user else None,
        service_account_id=uuid4() if service_account else None,
        parent_id=uuid4() if parent else None,
        config=config, created_at=now, updated_at=now,
    )


async def _offered(monkeypatch, session: Session, *, loop: bool = True) -> set[str]:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(100, 300))
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store)
    harness._tools = registry
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
    _session(parent=True, scheduled_session_id=str(uuid4())),
    _session(scheduled_session_id=str(uuid4())),
    _session(parent=True, workstream_role="thread"),
    _session(parent=True),
    _session(memory_boundary=f"{EVAL_BOUNDARY_PREFIX}run-1"),
], ids=["anonymous", "a-scheduled-run", "a-detached-scheduled-run", "a-project-thread", "a-worker", "an-eval-row"])
@pytest.mark.asyncio
async def test_a_session_no_one_owns_or_answers_is_not(monkeypatch, session):
    assert not ROUTINE_TOOLS & await _offered(monkeypatch, session)


@pytest.mark.asyncio
async def test_an_agent_without_loop_is_not(monkeypatch):
    assert not ROUTINE_TOOLS & await _offered(monkeypatch, _session(), loop=False)
