"""A reply from an integrator's app passes the same gates as any channel's."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from surogates.channels import api_inbound
from surogates.db.models import ChannelIdentity, Event
from surogates.followup import store as followup_store
from surogates.runtime.platform_client import AllowanceExhaustedError

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT = "agent-api"


async def _person(sf, org_user):
    org_id, user_id = org_user
    external = f"ext-{uuid.uuid4().hex[:8]}"
    async with sf() as db:
        db.add(ChannelIdentity(org_id=org_id, user_id=user_id, platform="api", platform_user_id=external,
                               platform_meta={"contact_permission": {"status": "granted"}}))
        await db.commit()
    return org_id, user_id, external


async def _messages(sf, session_id):
    async with sf() as db:
        return [d for d in (await db.execute(
            sa.select(Event.data).where(Event.session_id == uuid.UUID(session_id))
            .where(Event.type == "user.message").order_by(Event.id)
        )).scalars()]


async def test_a_form_is_kept_as_a_forms_answers(session_factory, org_and_user):
    org_id, _user, external = await _person(session_factory, org_and_user)
    result = await api_inbound.receive(session_factory, None, org_id=org_id, agent_id=AGENT,
                                       external_id=external, text="", form={"pain": "3", "fever": "no"})
    [event] = await _messages(session_factory, result["session_id"])
    assert event["source"]["flow_reply"] is True
    assert event["source"]["flow_fields"] == {"pain": "3", "fever": "no"}
    assert "pain" in event["content"]


class _Refusing:
    async def allowance_authorize(self, agent_id, **kw):
        raise AllowanceExhaustedError("subscription_required")


async def test_the_plan_gate_refuses_and_keeps_the_message_held(session_factory, org_and_user):
    org_id, _user, external = await _person(session_factory, org_and_user)
    result = await api_inbound.receive(
        session_factory, None, org_id=org_id, agent_id=AGENT, external_id=external, text="hi",
        platform_client=_Refusing(), runtime_payload={"end_user_token_allowance": 1000},
    )
    assert result["refused"] == "subscription_required"
    assert result["notice"].startswith("A subscription is required")
    [event] = await _messages(session_factory, result["session_id"])
    assert event["held"] == "subscription_required"


async def test_stop_from_the_person_withdraws_the_api_channel(session_factory, org_and_user):
    org_id, user_id, external = await _person(session_factory, org_and_user)
    await api_inbound.receive(session_factory, None, org_id=org_id, agent_id=AGENT,
                              external_id=external, text="STOP")
    async with session_factory() as db:
        identity = (await db.execute(sa.select(ChannelIdentity).where(
            ChannelIdentity.platform_user_id == external))).scalar_one()
    assert followup_store.permission_status(identity) == "withdrawn"


async def test_a_pending_question_is_answered_or_dismissed(session_factory, org_and_user, monkeypatch):
    from surogates.session import interactive_input

    org_id, user_id, external = await _person(session_factory, org_and_user)
    pending = {"user_id": str(user_id), "tool_call_id": "call-1",
               "questions": [{"question": "Ok?", "choices": [{"label": "Da"}, {"label": "Nu"}]}]}

    async def lookup(store, *, session_id):
        return pending

    resolved = []

    async def resolve(store, *, session_id, tool_call_id, responses):
        resolved.append(tool_call_id)
        return True

    monkeypatch.setattr(interactive_input, "pending_input_for_session", lookup)
    monkeypatch.setattr(interactive_input, "resolve_input_response", resolve)
    answered = await api_inbound.receive(session_factory, None, org_id=org_id, agent_id=AGENT,
                                         external_id=external, text="Da")
    assert resolved == ["call-1"] and await _messages(session_factory, answered["session_id"]) == []

    other = await api_inbound.receive(session_factory, None, org_id=org_id, agent_id=AGENT,
                                      external_id=external, text="pot să iau aspirină?")
    [event] = await _messages(session_factory, other["session_id"])
    from surogates.tools.builtin.ask_user_question import DISMISSES_QUESTION

    assert event[DISMISSES_QUESTION] == "call-1"
