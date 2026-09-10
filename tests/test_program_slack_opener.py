"""Opening a Slack direct message with the Program's plain-text opener."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from surogates.programs.materialize import send_queued_openers
from surogates.programs.opener import OpenerUndeliverable, make_opener_enqueue


def _utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def identity_lookup():
    async def _granted(org_id, platform, user_id):
        return SimpleNamespace(
            platform_user_id="U1",
            platform_meta={"contact_permission": {"status": "granted"}},
        )

    return _granted


class _SlackClient:
    def __init__(self, dm="D777", fail=None):
        self.dm, self.fail, self.opened = dm, fail, []

    async def conversations_open(self, *, users):
        self.opened.append(users)
        if self.fail:
            raise RuntimeError(self.fail)
        return {"channel": {"id": self.dm}}


class _Store:
    def __init__(self):
        self.emitted = []

    async def emit_synthetic_user_message(self, session_id, **kw):
        self.emitted.append((session_id, kw))
        return 42


class _Delivery:
    def __init__(self):
        self.calls = []

    async def enqueue(self, session_id, event_id, channel, destination, payload):
        self.calls.append((session_id, event_id, channel, destination, payload))
        return 99


def _slack_enqueue(monkeypatch, *, client, creds=None, config=None):
    from surogates.programs import opener as opener_mod

    session_id = uuid.uuid4()
    captured = {}

    async def _session(store, redis, *, session_key, config, **kw):
        captured["key"], captured["config"] = session_key, config
        return session_id

    monkeypatch.setattr(opener_mod, "get_or_create_channel_session", _session)

    async def _config(program_id):
        return config if config is not None else {
            "channel": "slack", "channel_identifier": "A0123",
            "opener_text": "Time for your check-in. Reply to begin.",
        }

    async def _creds(kind, identifier, org_id):
        assert (kind, identifier) == ("slack", "A0123")
        return creds if creds is not None else {"bot_token": "xoxb-1"}

    delivery = _Delivery()
    store = _Store()
    enqueue = make_opener_enqueue(
        session_store=store, redis=None, session_factory=None,
        delivery_service=delivery, config_for_program=_config,
        credentials_for=_creds, slack_client_factory=lambda token: client,
    )
    captured["store"] = store
    return enqueue, delivery, captured, session_id


@pytest.mark.asyncio
async def test_the_slack_opener_lands_in_the_direct_message_session(
    monkeypatch, make_invitation,
):
    inv = await make_invitation(
        delivery_state="queued", response_state="not_started",
        platform="slack", platform_user_id="U1",
    )
    client = _SlackClient(dm="D777")
    enqueue, delivery, captured, _sid = _slack_enqueue(monkeypatch, client=client)
    assert await enqueue(inv) == 99
    # Inbound keys a Slack DM session on the D-channel id, so the opener must
    # too: the same key the reply pipeline computes for a top-level DM.
    from surogates.channels.source import SessionSource, build_session_key

    assert client.opened == ["U1"]
    assert captured["key"] == build_session_key(SessionSource(
        platform="slack", chat_id="D777", chat_type="dm", user_id="U1",
        user_name="", thread_id=None, chat_name="D777",
    ))
    # The transcript event is user-role; the opener is marked as the agent's
    # own message, not something the person said.
    ((event_sid, event), ) = captured["store"].emitted
    assert event_sid == _sid
    assert event["content"] == "[Check-in opener sent: Time for your check-in. Reply to begin.]"
    assert event["synthetic"] == "checkin_opener"
    assert event["metadata"]["program_id"] == str(inv.program_id)
    assert captured["config"]["slack_channel_id"] == "D777"
    assert captured["config"]["channel_identifier"] == "A0123"
    ((_, event_id, channel, destination, payload),) = delivery.calls
    assert (event_id, channel) == (42, "slack")
    assert destination == {
        "channel_id": "D777", "thread_ts": None, "channel_identifier": "A0123",
    }
    assert payload == {"content": "Time for your check-in. Reply to begin."}


@pytest.mark.asyncio
async def test_a_member_slack_cannot_reach_is_undeliverable(monkeypatch, make_invitation):
    inv = await make_invitation(
        delivery_state="queued", response_state="not_started",
        platform="slack", platform_user_id="U9",
    )
    enqueue, *_ = _slack_enqueue(monkeypatch, client=_SlackClient(fail="user_not_found"))
    with pytest.raises(OpenerUndeliverable) as exc:
        await enqueue(inv)
    assert "user_not_found" in exc.value.reason


@pytest.mark.asyncio
async def test_a_missing_bot_token_is_undeliverable(monkeypatch, make_invitation):
    inv = await make_invitation(
        delivery_state="queued", response_state="not_started",
        platform="slack", platform_user_id="U1",
    )
    enqueue, *_ = _slack_enqueue(monkeypatch, client=_SlackClient(), creds={})
    with pytest.raises(OpenerUndeliverable):
        await enqueue(inv)


@pytest.mark.asyncio
async def test_an_undeliverable_opener_fails_its_user_visibly(
    sf, make_invitation, identity_lookup, reload_invitation,
):
    # Not "left queued and retried every tick": the row says failed and why,
    # and the user is free for the next occurrence.
    inv = await make_invitation(
        delivery_state="queued", response_state="not_started",
        platform="slack", platform_user_id="U9",
    )

    async def _enqueue(row):
        raise OpenerUndeliverable("Slack: user_not_found")

    await send_queued_openers(
        sf, identity_lookup=identity_lookup, now=_utcnow(), enqueue=_enqueue,
    )
    row = await reload_invitation(inv.id)
    assert row.delivery_state == "failed"
    assert row.delivery_error == "Slack: user_not_found"
    assert row.response_state == "not_started"
    assert row.outbox_id is None
