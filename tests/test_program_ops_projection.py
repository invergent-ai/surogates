"""Fetching ops' active-Program projection."""

from __future__ import annotations

import uuid

import pytest

from surogates.programs.ops_projection import fetch_active_programs


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._body


class _Client:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        if self._raises is not None:
            raise self._raises
        return self._response


def _row():
    return {
        "id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "agent_id": "a1",
        "skill_ref": "post-op",
        "channel": "whatsapp",
        "channel_identifier": "127",
        "template_name": "daily",
        "template_language": "en_US",
        "weekdays": ["mon"],
        "times_local": ["09:00"],
        "timezone": "UTC",
        "response_deadline_hours": 24,
        "escalation_service_account_id": str(uuid.uuid4()),
        "patients": [str(uuid.uuid4())],
    }


@pytest.mark.asyncio
async def test_it_sends_the_runtime_key_and_returns_the_rows():
    row = _row()
    client = _Client(_Resp(200, [row]))
    got = await fetch_active_programs(
        client, base_url="http://ops:8888", runtime_key="rk_secret",
    )
    assert got == [row]

    url, kw = client.calls[0]
    assert url == "http://ops:8888/api/programs/active"
    # The endpoint is runtime-scoped; without the bearer it answers 401 and
    # every Program would silently stop being reconciled.
    assert kw["headers"]["Authorization"] == "Bearer rk_secret"


@pytest.mark.asyncio
async def test_a_trailing_slash_does_not_double_up():
    client = _Client(_Resp(200, []))
    await fetch_active_programs(
        client, base_url="http://ops:8888/", runtime_key="rk",
    )
    assert client.calls[0][0] == "http://ops:8888/api/programs/active"


@pytest.mark.asyncio
async def test_an_unreachable_ops_returns_none_not_an_empty_list():
    # The distinction is the whole point. An empty list means "ops says no
    # Programs are active", which reconcile answers by deactivating every
    # schedule. A failed fetch must never be able to say that.
    client = _Client(raises=RuntimeError("connection refused"))
    assert await fetch_active_programs(
        client, base_url="http://ops:8888", runtime_key="rk",
    ) is None


@pytest.mark.asyncio
async def test_a_rejected_fetch_returns_none():
    client = _Client(_Resp(401, {"detail": "unauthorized"}))
    assert await fetch_active_programs(
        client, base_url="http://ops:8888", runtime_key="rk",
    ) is None


@pytest.mark.asyncio
async def test_a_non_list_body_returns_none():
    # A proxy error page answering 200 must not be read as "no Programs".
    client = _Client(_Resp(200, {"detail": "gateway"}))
    assert await fetch_active_programs(
        client, base_url="http://ops:8888", runtime_key="rk",
    ) is None


@pytest.mark.asyncio
async def test_an_empty_projection_is_a_real_answer():
    client = _Client(_Resp(200, []))
    assert await fetch_active_programs(
        client, base_url="http://ops:8888", runtime_key="rk",
    ) == []
