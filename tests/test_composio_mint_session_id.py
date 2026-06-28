"""The proxy forwards session_id into the Composio mint request body."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from surogates.mcp_proxy.loader import apply_composio_minting
from surogates.runtime.platform_client import PlatformClient


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self):
        self.posted = []

    async def post(self, url, json=None):
        self.posted.append((url, json))
        return _FakeResp({"transport": "http", "url": "https://x", "headers": {}})


@pytest.mark.asyncio
async def test_mint_sends_session_id_in_body():
    pc = PlatformClient.__new__(PlatformClient)  # skip __init__
    pc._client = _FakeHttp()
    sid = str(uuid.uuid4())

    await pc.mint_composio_session("a1", "sender", session_id=sid)

    _url, body = pc._client.posted[0]
    assert body == {"user_id": "sender", "session_id": sid}


@pytest.mark.asyncio
async def test_mint_omits_session_id_when_absent():
    pc = PlatformClient.__new__(PlatformClient)
    pc._client = _FakeHttp()

    await pc.mint_composio_session("a1", "sender")

    _url, body = pc._client.posted[0]
    assert body == {"user_id": "sender"}  # backward-compatible body


@pytest.mark.asyncio
async def test_apply_composio_minting_forwards_session_id():
    pc = AsyncMock()
    pc.mint_composio_session.return_value = {
        "transport": "http",
        "url": "https://x",
        "headers": {},
    }

    await apply_composio_minting(
        {"composio-gmail": {"transport": "composio"}},
        platform_client=pc,
        agent_id="a1",
        user_id="sender",
        session_id="sid-1",
    )

    pc.mint_composio_session.assert_awaited_once_with(
        "a1", "sender", session_id="sid-1",
    )
