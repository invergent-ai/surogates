"""Discord platform: parse, send, edit, files, typing, permanent errors."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from surogates.channels.channel_media import OutboundFile
from surogates.channels.delivery import is_permanent_delivery_error
from surogates.channels.platforms import discord as discord_mod
from surogates.channels.platforms.discord import DISCORD_API_BASE, DiscordPlatform, parse

BOT = "900000000000000001"
ALICE = "700000000000000101"
OTHER_BOT = "700000000000000999"
CHANNEL = "800000000000000011"
TOKEN = "fake-bot-token"
CREDS = {"bot_token": TOKEN}


def _event(content="hello", *, guild=True, author=ALICE, bot=False, mentions=(), **extra) -> dict:
    d = {
        "id": "1400000000000000001",
        "type": 0,
        "channel_id": CHANNEL,
        "author": {"id": author, "username": "alice", "global_name": "Alice", "bot": bot},
        "content": content,
        "mentions": [{"id": m} for m in mentions],
        "attachments": [],
        **extra,
    }
    if guild:
        d["guild_id"] = "800000000000000010"
        d.setdefault("member", {"nick": "Ali"})
    return {"t": "MESSAGE_CREATE", "d": d, "bot_user_id": BOT}


def _item(content="hi", **destination) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="s1",
        destination={"channel_id": CHANNEL, "channel_identifier": "app", **destination},
        payload={"content": content},
    )


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_a_dm_is_a_dm_keyed_on_its_channel():
    msg = parse(_event(guild=False), bot_user_id=BOT)
    assert msg.is_dm and msg.visibility == "dm" and msg.identifier == CHANNEL
    assert msg.platform_user_id == ALICE and msg.user_name == "Alice"
    assert msg.ts == "discord:1400000000000000001" and msg.thread_key is None


def test_a_server_message_without_a_mention_is_not_a_mention():
    msg = parse(_event(), bot_user_id=BOT)
    assert not msg.is_dm and not msg.is_mention and msg.visibility == "private"
    assert msg.user_name == "Ali" and msg.source["guild_id"] == "800000000000000010"


def test_a_mention_is_seen_and_stripped_in_both_forms():
    msg = parse(_event(f"<@{BOT}> hi <@!{BOT}>  there", mentions=[BOT]), bot_user_id=BOT)
    assert msg.is_mention and msg.text == "hi there"


def test_a_reply_to_the_bot_counts_as_a_mention():
    ref = {"referenced_message": {"author": {"id": BOT}}, "type": 19}
    assert parse(_event("thanks", **ref), bot_user_id=BOT).is_mention


def test_own_messages_and_unknown_bot_id_are_dropped():
    assert parse(_event(author=BOT, bot=True), bot_user_id=BOT) is None
    assert parse(_event(), bot_user_id="") is None


def test_other_bots_come_through_marked():
    msg = parse(_event(author=OTHER_BOT, bot=True), bot_user_id=BOT)
    assert msg is not None and msg.is_bot


def test_system_messages_and_blank_content_are_dropped():
    assert parse(_event(type=7), bot_user_id=BOT) is None  # member join
    assert parse(_event(""), bot_user_id=BOT) is None  # no Message Content intent
    assert parse({"t": "GUILD_CREATE", "d": {}, "bot_user_id": BOT}, bot_user_id=BOT) is None


def test_attachments_become_file_refs():
    att = {"id": "55", "filename": "a.png", "content_type": "image/png", "size": 3,
           "url": "https://cdn.discordapp.com/attachments/1/55/a.png"}
    msg = parse(_event("", attachments=[att]), bot_user_id=BOT)
    assert msg.kind == "image"
    (f,) = msg.files
    assert (f.url, f.filename, f.mime_type, f.size, f.file_id) == (att["url"], "a.png", "image/png", 3, "55")


async def test_platform_parse_reads_the_bot_id_the_gateway_attached():
    msg = await DiscordPlatform().parse(_event(guild=False))
    assert msg is not None and msg.is_dm


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


@respx.mock
async def test_send_splits_at_2000_and_only_the_first_chunk_replies():
    route = respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages").mock(
        side_effect=[httpx.Response(200, json={"id": "m1"}), httpx.Response(200, json={"id": "m2"})],
    )
    result = await DiscordPlatform().send(_item("x" * 3000, reply_to_message_id="r9"), creds=CREDS)

    assert result.success and result.message_id == "m2"
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert len(first["content"]) <= 2000 and len(second["content"]) <= 2000
    assert first["message_reference"] == {"message_id": "r9", "fail_if_not_exists": False}
    assert "message_reference" not in second
    assert first["allowed_mentions"]["parse"] == []
    assert route.calls[0].request.headers["authorization"] == f"Bot {TOKEN}"


@respx.mock
async def test_send_with_update_ts_edits_in_place():
    patch = respx.patch(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages/p1").mock(
        return_value=httpx.Response(200, json={"id": "p1"}),
    )
    post = respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages")
    result = await DiscordPlatform().send(_item("done", update_ts="p1"), creds=CREDS)
    assert result.success and result.message_id == "p1"
    assert json.loads(patch.calls[0].request.content)["content"] == "done"
    assert not post.called


@respx.mock
async def test_a_429_is_a_retryable_failure_without_waiting(monkeypatch):
    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(discord_mod.asyncio, "sleep", _sleep)
    route = respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages").mock(
        return_value=httpx.Response(429, json={"retry_after": 30}),
    )
    result = await DiscordPlatform().send(_item(), creds=CREDS)
    assert not result.success and result.retryable
    assert not is_permanent_delivery_error(result.error)
    assert slept == [] and route.call_count == 1


@respx.mock
async def test_a_2xx_body_that_is_not_json_does_not_raise():
    respx.post(f"{DISCORD_API_BASE}/users/@me/channels").mock(
        return_value=httpx.Response(200, text="<html>proxy</html>"),
    )
    async with httpx.AsyncClient() as http:
        assert await discord_mod._request(http, "POST", "/users/@me/channels", token=TOKEN) == ({}, "", False)


@pytest.mark.parametrize("code", [10003, 50001, 50007, 50013])
@respx.mock
async def test_dead_ends_are_permanent(code):
    respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages").mock(
        return_value=httpx.Response(403, json={"code": code, "message": "nope"}),
    )
    result = await DiscordPlatform().send(_item(), creds=CREDS)
    assert not result.success and is_permanent_delivery_error(result.error)


def test_other_codes_are_not_permanent():
    assert not is_permanent_delivery_error("discord error 50035 (Invalid Form Body)")
    assert not is_permanent_delivery_error("discord http 502 (Bad Gateway)")


@respx.mock
async def test_files_go_as_multipart():
    route = respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages").mock(
        return_value=httpx.Response(200, json={"id": "f1"}),
    )
    sent = await DiscordPlatform().send_files(
        _item(), creds=CREDS, files=[OutboundFile("r.pdf", "application/pdf", b"%PDF")],
    )
    assert sent == ["f1"]
    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="files[0]"; filename="r.pdf"' in body and b"%PDF" in body
    assert b'"filename": "r.pdf"' in body  # payload_json attachments


@respx.mock
async def test_send_private_opens_a_dm_from_a_server_channel():
    respx.post(f"{DISCORD_API_BASE}/users/@me/channels").mock(
        return_value=httpx.Response(200, json={"id": "dm1", "type": 1}),
    )
    post = respx.post(f"{DISCORD_API_BASE}/channels/dm1/messages").mock(
        return_value=httpx.Response(200, json={"id": "m"}),
    )
    assert await DiscordPlatform().send_private(
        CREDS, sender_id=ALICE, chat_id=CHANNEL, is_dm=False, text="link: 123",
    )
    assert post.called


@respx.mock
async def test_typing_repeats_until_the_answer_goes_out(monkeypatch):
    monkeypatch.setattr(discord_mod, "_TYPING_EVERY_S", 0.01)
    typing = respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/typing").mock(
        return_value=httpx.Response(204),
    )
    respx.post(f"{DISCORD_API_BASE}/channels/{CHANNEL}/messages").mock(
        return_value=httpx.Response(200, json={"id": "m"}),
    )
    platform = DiscordPlatform()

    async def _active():
        return True

    platform.keep_typing(SimpleNamespace(identifier=CHANNEL), creds=CREDS, turn_active=_active)
    await asyncio.sleep(0.05)
    assert typing.call_count >= 2
    await platform.send(_item("answer"), creds=CREDS)
    await asyncio.sleep(0)
    seen = typing.call_count
    await asyncio.sleep(0.05)
    assert typing.call_count == seen and not platform._typing


@respx.mock
async def test_an_oversized_attachment_stops_downloading_at_the_cap():
    url = "https://cdn.discordapp.com/attachments/1/2/big.bin"
    served: list[int] = []

    async def _body():
        for n in range(100):
            served.append(n)
            yield b"x" * 10

    respx.get(url).mock(return_value=httpx.Response(200, content=_body()))
    assert await DiscordPlatform().download_file(creds=CREDS, url=url, max_bytes=25) is None
    assert len(served) < 100

    respx.get(url).mock(return_value=httpx.Response(200, content=b"%PDF"))
    assert await DiscordPlatform().download_file(creds=CREDS, url=url, max_bytes=25) == b"%PDF"
