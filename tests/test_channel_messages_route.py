from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.routes import channel_files
from surogates.api.routes.channel_files import _ChannelMessagesBody


class _Store:
    def __init__(self, session):
        self._session = session

    async def get_session(self, session_id):
        return self._session


def _session(*, channel="slack", config=None):
    return SimpleNamespace(
        id=uuid4(), org_id=uuid4(), agent_id="a1", parent_id=None,
        channel=channel,
        config=config if config is not None
        else {"channel_identifier": "T1", "slack_channel_id": "C1"},
    )


class _Tenant:
    def __init__(self, owns=True):
        self._owns = owns

    def owns_session(self, org_id, session_id):
        return self._owns


def _request(session):
    state = SimpleNamespace(session_store=_Store(session), credential_vault=object())
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_messages_route_happy_path(monkeypatch):
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _fake(**kwargs):
        return {"messages_block": "…hi…", "count": 1, "channel": "surogate", "note": None}

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _fake)
    out = await channel_files.fetch_channel_messages_route(
        session_id=session.id, body=_ChannelMessagesBody(limit=10),
        request=_request(session), tenant=_Tenant())
    assert out["count"] == 1
    assert out["messages_block"]


async def test_messages_route_non_slack_400():
    session = _session(channel="web")
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(),
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 400


async def test_messages_route_foreign_session_404():
    session = _session(channel="slack")
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(),
            request=_request(session), tenant=_Tenant(owns=False))
    assert ei.value.status_code == 404


async def test_messages_route_invalid_since_400(monkeypatch):
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _raise(**kwargs):
        raise ValueError("bad since")

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _raise)
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_messages_route(
            session_id=session.id, body=_ChannelMessagesBody(since="banana"),
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 400


async def test_messages_route_missing_config_empty(monkeypatch):
    session = _session(channel="slack", config={})
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _fake(**kwargs):
        return {"messages_block": None, "count": 0, "channel": "",
                "note": "This session is not bound to a Slack channel."}

    monkeypatch.setattr(channel_files, "fetch_channel_messages", _fake)
    out = await channel_files.fetch_channel_messages_route(
        session_id=session.id, body=_ChannelMessagesBody(),
        request=_request(session), tenant=_Tenant())
    assert out["count"] == 0
    assert out["note"]
