from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.api.routes import channel_files
from surogates.channels.file_fetch import (
    ChannelFileForbidden,
    ChannelFileNotFound,
)


class _Store:
    def __init__(self, session):
        self._session = session

    async def get_session(self, session_id):
        return self._session


def _session(*, channel="slack", bucket="b"):
    return SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        channel=channel,
        config={"channel_identifier": "T1", "slack_channel_id": "C1",
                "storage_bucket": bucket},
    )


class _Tenant:
    def __init__(self, owns=True):
        self._owns = owns
        self.service_account_id = uuid4()

    def owns_session(self, org_id, session_id):
        return self._owns


def _request(session):
    state = SimpleNamespace(
        session_store=_Store(session),
        storage=object(),
        credential_vault=object(),
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        url=SimpleNamespace(path="/v1/sessions/x/channel-files/F1"),
    )


async def test_route_happy_path(monkeypatch):
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _fake_fetch(**kwargs):
        assert kwargs["file_id"] == "F1"
        assert kwargs["bucket"] == "b"
        return {"kind": "attachment", "path": "uploads/slack/fetch/F1-x.html",
                "filename": "x.html", "mime_type": "text/html", "size": 3,
                "inlined_text": "hi"}

    monkeypatch.setattr(channel_files, "fetch_channel_file", _fake_fetch)
    out = await channel_files.fetch_channel_file_route(
        session_id=session.id, file_id="F1",
        request=_request(session), tenant=_Tenant())
    assert out["path"].endswith("x.html")
    assert out["inlined_text"] == "hi"


async def test_route_ambient_resolves_to_slack(monkeypatch):
    session = _session(channel="ambient")
    seen = {}
    monkeypatch.setattr(channel_files.registry, "get",
                        lambda kind: seen.setdefault("kind", kind) or object())

    async def _fake_fetch(**kwargs):
        return {"kind": "attachment", "path": "p", "filename": "f",
                "mime_type": "text/plain", "size": 1}

    monkeypatch.setattr(channel_files, "fetch_channel_file", _fake_fetch)
    await channel_files.fetch_channel_file_route(
        session_id=session.id, file_id="F1",
        request=_request(session), tenant=_Tenant())
    assert seen["kind"] == "slack"


async def test_route_rejects_non_slack_channel(monkeypatch):
    from fastapi import HTTPException
    session = _session(channel="web")
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_file_route(
            session_id=session.id, file_id="F1",
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 400


async def test_route_maps_forbidden_to_403(monkeypatch):
    from fastapi import HTTPException
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _raise(**_):
        raise ChannelFileForbidden("nope")

    monkeypatch.setattr(channel_files, "fetch_channel_file", _raise)
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_file_route(
            session_id=session.id, file_id="F1",
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 403


async def test_route_maps_not_found_to_404(monkeypatch):
    from fastapi import HTTPException
    session = _session(channel="slack")
    monkeypatch.setattr(channel_files.registry, "get", lambda kind: object())

    async def _raise(**_):
        raise ChannelFileNotFound("missing")

    monkeypatch.setattr(channel_files, "fetch_channel_file", _raise)
    with pytest.raises(HTTPException) as ei:
        await channel_files.fetch_channel_file_route(
            session_id=session.id, file_id="F1",
            request=_request(session), tenant=_Tenant())
    assert ei.value.status_code == 404


def test_slack_platform_registered_when_route_imported():
    # Importing the route module must guarantee SlackPlatform self-registers
    # in this (runtime-api) process; otherwise registry.get("slack") is None
    # and every fetch returns 500. The other route tests stub registry.get,
    # so only this unstubbed test catches the wiring.
    from surogates.api.routes import channel_files  # noqa: F401
    from surogates.channels.registry import registry
    assert registry.get("slack") is not None
