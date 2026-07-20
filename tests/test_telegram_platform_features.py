"""Tests for the Telegram platform upgrades: media parse, download, HTML
send with chunking/reply-threading, reaction ack, and interactive input."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from surogates.channels.platforms.telegram import TelegramPlatform, parse
from surogates.channels.platforms.telegram_interactive import (
    build_input_prompt,
    parse_callback_data,
    resolve_text_answer,
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


# ---------------------------------------------------------------------------
# Inbound media parse
# ---------------------------------------------------------------------------


class TestMediaParse:
    def test_photo_takes_largest_size(self):
        update = _message(
            {
                "photo": [
                    {"file_id": "small", "file_unique_id": "u1", "file_size": 100},
                    {"file_id": "big", "file_unique_id": "u2", "file_size": 9000},
                ]
            },
            caption="look",
        )
        msg = parse(update, bot_username=BOT)
        assert msg.text == "look"
        assert msg.kind == "image"
        assert [f.file_id for f in msg.files] == ["big"]
        assert msg.files[0].mime_type == "image/jpeg"

    def test_document_keeps_name_and_mime(self):
        update = _message(
            {
                "document": {
                    "file_id": "doc1",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 1234,
                }
            }
        )
        msg = parse(update, bot_username=BOT)
        assert msg.kind == "document"
        ref = msg.files[0]
        assert (ref.filename, ref.mime_type, ref.size) == ("report.pdf", "application/pdf", 1234)
        assert ref.url == "doc1"

    def test_voice_message(self):
        update = _message({"voice": {"file_id": "v1", "mime_type": "audio/ogg", "file_size": 10}})
        msg = parse(update, bot_username=BOT)
        assert msg.kind == "audio"
        assert msg.files[0].filename == "voice.ogg"

    def test_message_id_recorded_in_source(self):
        msg = parse(_message({}, text="hello"), bot_username=BOT)
        assert msg.source["message_id"] == 42


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# send — HTML, chunking, reply threading
# ---------------------------------------------------------------------------


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
        assert calls[0]["reply_parameters"] == {"message_id": 42}
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


# ---------------------------------------------------------------------------
# ack_received (reaction)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Interactive input
# ---------------------------------------------------------------------------


QUESTIONS = [{"prompt": "Deploy to prod?", "choices": [{"label": "Yes"}, {"label": "No"}], "allow_other": False}]


class TestInteractive:
    def test_build_prompt_single_choice_question(self):
        html_text, plain_text, markup = build_input_prompt(
            session_id="s-1", questions=QUESTIONS, context="Release 1.2 ready.",
        )
        assert "Deploy to prod?" in html_text and "Release 1.2" in plain_text
        rows = markup["inline_keyboard"]
        assert [r[0]["text"] for r in rows] == ["Yes", "No"]
        assert rows[0][0]["callback_data"] == "si:s-1:0:0"

    def test_callback_data_fits_telegram_cap(self):
        _, _, markup = build_input_prompt(
            session_id="11111111-2222-3333-4444-555555555555",
            questions=QUESTIONS,
        )
        for row in markup["inline_keyboard"]:
            assert len(row[0]["callback_data"].encode()) <= 64

    def test_free_text_prompt_has_no_keyboard(self):
        _, _, markup = build_input_prompt(
            session_id="s-1", questions=[{"prompt": "Name?"}],
        )
        assert markup is None

    def test_parse_callback_roundtrip(self):
        assert parse_callback_data("si:abc:0:1") == ("abc", 0, 1)
        assert parse_callback_data("nope") is None
        assert parse_callback_data("si:abc:x:1") is None

    def test_resolve_text_answer_matches_choice(self):
        responses = resolve_text_answer(QUESTIONS, "yes")
        assert responses[0]["answer"] == "Yes" and responses[0]["is_other"] is False

    def test_resolve_text_answer_other(self):
        responses = resolve_text_answer(QUESTIONS, "maybe later")
        assert responses[0]["answer"] == "maybe later" and responses[0]["is_other"] is True

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
        body = self._callback_body("si:11111111-2222-3333-4444-555555555555:0:1")
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
        body = self._callback_body("si:11111111-2222-3333-4444-555555555555:0:1")
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
