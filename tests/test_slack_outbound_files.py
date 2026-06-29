"""Tests for SlackPlatform outbound file upload + message delete.

A fake AsyncWebClient is injected into SlackPlatform._clients so no live Slack
calls happen. files_upload_v2 returns the modern {"files": [{"id": …}]} shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from surogates.channels.channel_media import OutboundFile
from surogates.channels.platforms.slack import SlackPlatform

TOKEN = "xoxb-test-token"


@dataclass
class _Item:
    destination: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    session_id: str = "sess-1"


class _FakeClient:
    def __init__(self, resp: Any = None, raises: Exception | None = None) -> None:
        self.uploads: list[dict] = []
        self.deletes: list[dict] = []
        self._resp = resp if resp is not None else {"files": [{"id": "F0001"}]}
        self._raises = raises

    async def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        if self._raises:
            raise self._raises
        return self._resp

    async def chat_delete(self, **kwargs):
        self.deletes.append(kwargs)


def _platform_with(client: _FakeClient) -> SlackPlatform:
    p = SlackPlatform()
    p._clients[TOKEN] = client
    return p


class TestSendFiles:
    async def test_uploads_each_file_and_returns_ids(self):
        client = _FakeClient(resp={"files": [{"id": "F123"}]})
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001", "thread_ts": "1700.1"})
        files = [
            OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a"),
            OutboundFile(filename="b.png", mime_type="image/png", data=b"b"),
        ]

        ids = await platform.send_files(item, creds={"bot_token": TOKEN}, files=files)

        assert len(client.uploads) == 2
        assert ids == ["F123", "F123"]
        first = client.uploads[0]
        assert first["channel"] == "C001"
        assert first["content"] == b"a"
        assert first["filename"] == "a.pdf"
        assert first["thread_ts"] == "1700.1"

    async def test_no_thread_ts_omitted(self):
        client = _FakeClient()
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        await platform.send_files(
            item, creds={"bot_token": TOKEN},
            files=[OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a")],
        )
        assert "thread_ts" not in client.uploads[0]

    async def test_upload_exception_skipped_no_raise(self):
        client = _FakeClient(raises=RuntimeError("missing_scope"))
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        ids = await platform.send_files(
            item, creds={"bot_token": TOKEN},
            files=[OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a")],
        )
        assert ids == []  # logged + skipped, not raised
        assert len(client.uploads) == 1  # upload was attempted before the swallowed exception

    async def test_missing_token_returns_empty(self):
        client = _FakeClient()
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        ids = await platform.send_files(
            item, creds={}, files=[OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a")],
        )
        assert ids == []
        assert client.uploads == []

    async def test_empty_files_returns_empty(self):
        client = _FakeClient()
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        ids = await platform.send_files(item, creds={"bot_token": TOKEN}, files=[])
        assert ids == []
        assert client.uploads == []

    async def test_uploads_with_singular_file_response_shape(self):
        client = _FakeClient(resp={"file": {"id": "F999"}})
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        ids = await platform.send_files(
            item, creds={"bot_token": TOKEN},
            files=[OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a")],
        )
        assert ids == ["F999"]

    async def test_malformed_response_shape_skipped_no_raise(self):
        client = _FakeClient(resp={"files": ["not-a-dict"]})
        platform = _platform_with(client)
        item = _Item(destination={"channel_id": "C001"})
        ids = await platform.send_files(
            item, creds={"bot_token": TOKEN},
            files=[OutboundFile(filename="a.pdf", mime_type="application/pdf", data=b"a")],
        )
        assert ids == []  # malformed shape caught and skipped, not raised


class TestDeleteMessage:
    async def test_calls_chat_delete(self):
        client = _FakeClient()
        platform = _platform_with(client)
        await platform.delete_message(creds={"bot_token": TOKEN}, channel="C001", ts="1700.1")
        assert client.deletes == [{"channel": "C001", "ts": "1700.1"}]

    async def test_swallows_errors(self):
        client = _FakeClient(raises=RuntimeError("message_not_found"))

        async def _boom(**kwargs):
            raise RuntimeError("message_not_found")

        client.chat_delete = _boom  # type: ignore[assignment]
        platform = _platform_with(client)
        # Must not raise.
        await platform.delete_message(creds={"bot_token": TOKEN}, channel="C001", ts="x")
        assert client.deletes == []  # _boom raised before recording; nothing stale landed

    async def test_missing_args_noop(self):
        client = _FakeClient()
        platform = _platform_with(client)
        await platform.delete_message(creds={"bot_token": TOKEN}, channel="", ts="1700.1")
        assert client.deletes == []
