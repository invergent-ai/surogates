"""Telegram media transfer, message delivery and pending-input interactions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import respx

from surogates.channels.platforms.telegram import TelegramPlatform, parse
from surogates.channels.platforms.telegram_interactive import (
    tool_call_digest,
)

BOT = "@my_test_bot"
API = "https://api.telegram.org"


def _message(extra: dict, *, text: str | None = None, caption: str | None = None):
    message = {
        "message_id": 42,
        "from": {"id": 1, "is_bot": False, "first_name": "Alice"},
        "chat": {"id": 111, "type": "private"},
        "date": 1700000005,
        **extra,
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 7, "message": message}


def _item(content: str = "hi", *, destination: dict | None = None, payload: dict | None = None):
    return SimpleNamespace(
        id=1,
        session_id="11111111-2222-3333-4444-555555555555",
        destination={"chat_id": "111", **(destination or {})},
        payload={"content": content, **(payload or {})},
    )


class TestDownloadFile:
    @respx.mock
    async def test_downloads_via_getfile(self):
        respx.post(f"{API}/bottok/getFile").mock(
            return_value=httpx.Response(200, json={
                "ok": True, "result": {"file_path": "photos/x.jpg", "file_size": 5},
            })
        )
        respx.get(f"{API}/file/bottok/photos/x.jpg").mock(
            return_value=httpx.Response(200, content=b"bytes")
        )
        p = TelegramPlatform()
        data = await p.download_file(creds={"bot_token": "tok"}, url="fid", max_bytes=100)
        assert data == b"bytes"

    @respx.mock
    async def test_rejects_oversize_declared(self):
        respx.post(f"{API}/bottok/getFile").mock(
            return_value=httpx.Response(200, json={
                "ok": True, "result": {"file_path": "p", "file_size": 999},
            })
        )
        p = TelegramPlatform()
        assert await p.download_file(creds={"bot_token": "tok"}, url="fid", max_bytes=10) is None

    @respx.mock
    async def test_none_on_api_error(self):
        respx.post(f"{API}/bottok/getFile").mock(
            return_value=httpx.Response(200, json={"ok": False, "description": "not found"})
        )
        p = TelegramPlatform()
        assert await p.download_file(creds={"bot_token": "tok"}, url="fid", max_bytes=10) is None

    async def test_none_without_token(self):
        p = TelegramPlatform()
        assert await p.download_file(creds={}, url="fid", max_bytes=10) is None


def _capture_send(route):
    return [json.loads(call.request.content) for call in route.calls]


class TestSend:
    @respx.mock
    async def test_sends_html(self):
        route = respx.post(f"{API}/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
        )
        p = TelegramPlatform()
        result = await p.send(_item("**bold** move"), creds={"bot_token": "tok"})
        assert result.success and result.message_id == "5"
        sent = _capture_send(route)[0]
        assert sent["text"] == "<b>bold</b> move"
        assert sent["parse_mode"] == "HTML"

    @respx.mock
    async def test_parse_error_retries_plain(self):
        route = respx.post(f"{API}/bottok/sendMessage")
        route.side_effect = [
            httpx.Response(200, json={"ok": False, "description": "Bad Request: can't parse entities"}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 6}}),
        ]
        p = TelegramPlatform()
        result = await p.send(_item("**x**"), creds={"bot_token": "tok"})
        assert result.success and result.message_id == "6"
        calls = _capture_send(route)
        assert "parse_mode" in calls[0] and "parse_mode" not in calls[1]
        assert calls[1]["text"] == "x"

    @respx.mock
    async def test_long_text_chunked_under_limit(self):
        route = respx.post(f"{API}/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        )
        p = TelegramPlatform()
        text = "para. " * 2000  # ~12k chars
        result = await p.send(_item(text), creds={"bot_token": "tok"})
        assert result.success
        calls = _capture_send(route)
        assert len(calls) >= 3
        for sent in calls:
            assert len(sent["text"]) <= p._MAX_MESSAGE_CHARS

    @respx.mock
    async def test_reply_parameters_on_first_chunk_only(self):
        route = respx.post(f"{API}/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        )
        p = TelegramPlatform()
        text = "para. " * 2000
        await p.send(
            _item(text, destination={"reply_to_message_id": 42}),
            creds={"bot_token": "tok"},
        )
        calls = _capture_send(route)
        assert calls[0]["reply_parameters"] == {
            "message_id": 42,
            "allow_sending_without_reply": True,
        }
        assert all("reply_parameters" not in c for c in calls[1:])

    @respx.mock
    async def test_thread_id_propagates(self):
        route = respx.post(f"{API}/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        )
        p = TelegramPlatform()
        await p.send(
            _item("hi", destination={"message_thread_id": 77}),
            creds={"bot_token": "tok"},
        )
        assert _capture_send(route)[0]["message_thread_id"] == 77


class TestAckReceived:
    @respx.mock
    async def test_reacts_when_enabled(self):
        route = respx.post(f"{API}/bottok/setMessageReaction").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        p = TelegramPlatform()
        msg = parse(_message({}, text="hello"), bot_username=BOT)
        await p.ack_received(msg, creds={"bot_token": "tok"}, config={"reactions_enabled": True})
        assert route.called
        sent = json.loads(route.calls[0].request.content)
        assert sent["message_id"] == 42 and sent["chat_id"] == "111"

    async def test_noop_when_disabled(self):
        p = TelegramPlatform()
        p._http = AsyncMock()
        msg = parse(_message({}, text="hello"), bot_username=BOT)
        await p.ack_received(msg, creds={"bot_token": "tok"}, config={})
        p._http.post.assert_not_awaited()


QUESTIONS = [{"prompt": "Deploy to prod?", "choices": [{"label": "Yes"}, {"label": "No"}], "allow_other": False}]


class TestInteractive:


    @respx.mock
    async def test_send_input_prompt_includes_keyboard(self):
        route = respx.post(f"{API}/bottok/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})
        )
        p = TelegramPlatform()
        item = _item(
            "", payload={"input_prompt": True, "questions": QUESTIONS, "context": "ctx"},
        )
        result = await p.send(item, creds={"bot_token": "tok"})
        assert result.success and result.message_id == "3"
        sent = _capture_send(route)[0]
        assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("si:")


class TestCallbackResolution:
    def _deps(self, session_config: dict | None = None):
        session = SimpleNamespace(config=session_config or {"telegram_channel_id": "111"})
        store = SimpleNamespace(get_session=AsyncMock(return_value=session))
        return SimpleNamespace(session_store=store), store

    def _callback_body(self, data: str, chat_id: int = 111):
        return {
            "update_id": 9,
            "callback_query": {
                "id": "cbq-1",
                "data": data,
                "from": {"id": 1},
                "message": {"message_id": 3, "chat": {"id": chat_id}},
            },
        }

    @respx.mock
    async def test_resolves_pending_input(self, monkeypatch):
        answer_route = respx.post(f"{API}/bottok/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        edit_route = respx.post(f"{API}/bottok/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        deps, store = self._deps()
        pending = {"tool_call_id": "tc-1", "questions": QUESTIONS, "context": ""}
        monkeypatch.setattr(
            "surogates.session.interactive_input.pending_input_for_session",
            AsyncMock(return_value=pending),
        )
        resolve = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "surogates.session.interactive_input.resolve_input_response", resolve,
        )

        p = TelegramPlatform()
        body = self._callback_body(
            f"si:11111111-2222-3333-4444-555555555555:0:1:{tool_call_digest('tc-1')}"
        )
        handled = await p.handle_non_message_update(
            body, routing=None, creds={"bot_token": "tok"}, deps=deps,
        )
        assert handled is True
        resolve.assert_awaited_once()
        kwargs = resolve.await_args.kwargs
        assert kwargs["tool_call_id"] == "tc-1"
        assert kwargs["responses"][0]["answer"] == "No"
        assert edit_route.called and answer_route.called

    @respx.mock
    async def test_stale_button_does_not_answer_newer_question(self, monkeypatch):
        respx.post(f"{API}/bottok/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        deps, _ = self._deps()
        # A NEWER question is pending than the one the button was built for.
        pending = {"tool_call_id": "tc-NEW", "questions": QUESTIONS, "context": ""}
        monkeypatch.setattr(
            "surogates.session.interactive_input.pending_input_for_session",
            AsyncMock(return_value=pending),
        )
        resolve = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "surogates.session.interactive_input.resolve_input_response", resolve,
        )
        p = TelegramPlatform()
        body = self._callback_body(
            f"si:11111111-2222-3333-4444-555555555555:0:1:{tool_call_digest('tc-OLD')}"
        )
        handled = await p.handle_non_message_update(
            body, routing=None, creds={"bot_token": "tok"}, deps=deps,
        )
        assert handled is True
        resolve.assert_not_awaited()

    @respx.mock
    async def test_chat_mismatch_is_rejected(self, monkeypatch):
        respx.post(f"{API}/bottok/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        deps, _ = self._deps({"telegram_channel_id": "999"})
        resolve = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "surogates.session.interactive_input.resolve_input_response", resolve,
        )
        p = TelegramPlatform()
        body = self._callback_body(
            f"si:11111111-2222-3333-4444-555555555555:0:1:{tool_call_digest('tc-1')}"
        )
        handled = await p.handle_non_message_update(
            body, routing=None, creds={"bot_token": "tok"}, deps=deps,
        )
        assert handled is True
        resolve.assert_not_awaited()

    @respx.mock
    async def test_non_input_callback_still_acked(self):
        route = respx.post(f"{API}/bottok/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        p = TelegramPlatform()
        body = self._callback_body("something-else")
        handled = await p.handle_non_message_update(
            body, routing=None, creds={"bot_token": "tok"}, deps=SimpleNamespace(session_store=None),
        )
        assert handled is True and route.called
