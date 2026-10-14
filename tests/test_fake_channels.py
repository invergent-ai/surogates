"""The local fake Slack/Telegram server speaks the protocol the runtime expects.

Inbound: what the fake posts must pass the runtime's own ``verify`` and
``parse``.  Outbound: the runtime's real clients (slack_sdk, the Telegram
platform) must work against the fake once the API base env vars point at it.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import uvicorn

from surogates.channels.platforms import slack, telegram

SECRET = "0123456789abcdef0123456789abcdef"
_spec = importlib.util.spec_from_file_location(
    "fake_channels", pathlib.Path(__file__).parents[1] / "scripts" / "fake_channels.py",
)
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)
fake.SLACK_SIGNING_SECRET = SECRET


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture
def captured(monkeypatch):
    """Webhooks the fake delivers, answered 200 without reaching a runtime."""
    seen: list[httpx.Request] = []

    async def post(url: str, raw: bytes, headers: dict) -> httpx.Response:
        seen.append(httpx.Request("POST", url, content=raw, headers=headers))
        return httpx.Response(200, json={})

    monkeypatch.setattr(fake, "_post", post)
    return seen


def _as_request(r: httpx.Request, **path_params) -> SimpleNamespace:
    return SimpleNamespace(headers=r.headers, path_params=path_params)


async def _call(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://fake") as c:
        return (await c.post(path, json=body)).json()


async def test_slack_message_is_signed_and_parses_as_a_dm(captured):
    await _call("/api/slack/send", {"member": "U0DRRADU01", "text": "Ce pacienți am azi?"})
    r = captured[-1]
    assert r.url.path == f"/slack/{fake.SLACK_APP_ID}"
    assert slack.verify(_as_request(r, app_id=fake.SLACK_APP_ID), r.content, creds={"signing_secret": SECRET}) is True
    assert slack.verify(_as_request(r, app_id=fake.SLACK_APP_ID), r.content, creds={"signing_secret": "x" * 32}) is False
    msg = slack.parse(json.loads(r.content), bot_user_id=fake.SLACK_BOT_USER)
    assert (msg.identifier, msg.platform_user_id, msg.is_dm, msg.text) == ("D0DRRADU01", "U0DRRADU01", True, "Ce pacienți am azi?")


async def test_slack_button_and_modal_payloads_verify(captured):
    from surogates.channels.platforms.slack_interactive import (
        build_input_prompt_blocks, build_question_modal, parse_modal_submission,
    )

    questions = [{"prompt": "Cum vă simțiți?", "choices": [{"label": "Bine"}, {"label": "Rău"}]}]
    text, blocks = build_input_prompt_blocks(session_id="s1", tool_call_id="t1", questions=questions)
    fake.state["messages"].append({"platform": "slack", "dir": "out", "peer": "U0DRRADU01", "ts": time.time(),
                                   "id": "1.000001", "channel": "D0DRRADU01", "text": text, "blocks": blocks})
    assert (await _call("/api/slack/tap", {"member": "U0DRRADU01", "label": "Answer"}))["ok"]
    r = captured[-1]
    assert r.url.path.endswith("/interact")
    assert slack.verify(_as_request(r, app_id=fake.SLACK_APP_ID), r.content, creds={"signing_secret": SECRET}) is True
    payload = json.loads(parse_qs(r.content.decode())["payload"][0])
    assert payload["actions"][0]["action_id"] == "surogates_input_answer"
    assert payload["container"]["message_ts"] == "1.000001"

    view = build_question_modal(session_id="s1", tool_call_id="t1", questions=questions)
    fake.state["slack"]["modals"]["U0DRRADU01"] = {**view, "id": "V1"}
    await _call("/api/slack/modal", {"member": "U0DRRADU01", "values": {"q0_choice": "Rău"}})
    submitted = json.loads(parse_qs(captured[-1].content.decode())["payload"][0])
    parsed = parse_modal_submission(submitted["view"], questions)
    assert parsed.responses == [{"question": "Cum vă simțiți?", "answer": "Rău", "is_other": False}]


async def test_telegram_update_carries_the_webhook_secret(captured):
    fake.state["telegram"]["webhook"] = {"url": "http://runtime/telegram/@pretorian_fake_bot", "secret_token": "s3cr3t"}
    await _call("/api/telegram/send", {"user": "7100000001", "text": "/start"})
    r = captured[-1]
    assert telegram.verify(_as_request(r), r.content, creds={"webhook_secret": "s3cr3t"}) is True
    assert telegram.verify(_as_request(r), r.content, creds={"webhook_secret": "other"}) is False
    msg = telegram.parse(json.loads(r.content), bot_username="@pretorian_fake_bot")
    assert (msg.identifier, msg.platform_user_id, msg.is_dm, msg.text) == ("7100000001", "7100000001", True, "/start")


async def test_slack_sdk_against_the_fake(server, monkeypatch, captured):
    monkeypatch.setattr(slack, "SLACK_API_BASE", f"{server}/slack/api/")
    client = slack.web_client("xoxb-fake-pretorian")
    assert (await client.auth_test())["user_id"] == fake.SLACK_BOT_USER
    dm = (await client.conversations_open(users="U0DRRADU01"))["channel"]["id"]
    posted = await client.chat_postMessage(channel=dm, text="_Thinking…_")
    await client.chat_update(channel=dm, ts=posted["ts"], text="Aveți 2 pacienți noi.")
    up = await client.files_upload_v2(channel=dm, content=b"raport", filename="raport.txt", title="raport.txt")
    assert up["files"][0]["id"]
    names = [m["real_name"] for m in (await client.users_list())["members"]]
    assert "Dr. Radu Pretorian" in names
    thread = httpx.get(f"{server}/api/slack/thread", params={"member": "U0DRRADU01"}).json()["messages"]
    assert thread[-2]["text"] == "Aveți 2 pacienți noi." and thread[-2]["edited"]
    assert thread[-1]["files"][0]["name"] == "raport.txt"

    platform = slack.SlackPlatform()
    monkeypatch.setenv("SUROGATES_SLACK_API_BASE", f"{server}/slack/api/")
    async with httpx.AsyncClient() as c:
        await c.post(f"{server}/api/slack/send", json={"member": "U0DRRADU01", "media_b64": "aGVsbG8=", "filename": "a.txt"})
    url = json.loads(captured[-1].content)["event"]["files"][0]["url_private_download"]
    assert await platform.download_file(creds={"bot_token": "xoxb-x"}, url=url, max_bytes=100) == b"hello"


async def test_telegram_platform_against_the_fake(server, monkeypatch, captured):
    monkeypatch.setattr(telegram, "TELEGRAM_API_BASE", f"{server}/telegram")
    token = "7000000001:AAFakeLocalStackTelegramToken"
    await telegram._register_webhook_impl("@pretorian_fake_bot", "http://runtime/telegram/@pretorian_fake_bot",
                                          {"bot_token": token, "webhook_secret": "abc"})
    assert fake.state["telegram"]["webhook"]["secret_token"] == "abc"

    platform = telegram.TelegramPlatform()
    item = SimpleNamespace(destination={"chat_id": "7100000101"}, payload={"content": "Bună ziua"})
    refused = await platform.send(item, creds={"bot_token": token})
    assert not refused.success and "initiate" in refused.error

    from surogates.channels.platforms.telegram_interactive import build_input_prompt
    html, _, markup = build_input_prompt(session_id="00000000-0000-0000-0000-000000000001", tool_call_id="t1",
                                         questions=[{"prompt": "Ok?", "choices": [{"label": "Da"}, {"label": "Nu"}]}])
    item = SimpleNamespace(session_id="00000000-0000-0000-0000-000000000001", destination={"chat_id": "7100000001"},
                           payload={"input_prompt": True, "tool_call_id": "t1",
                                    "questions": [{"prompt": "Ok?", "choices": [{"label": "Da"}, {"label": "Nu"}]}]})
    sent = await platform.send(item, creds={"bot_token": token})
    assert sent.success
    assert (await _call("/api/telegram/tap", {"user": "7100000001", "label": "Da"}))["ok"]
    cb = json.loads(captured[-1].content)["callback_query"]
    assert cb["data"] == markup["inline_keyboard"][0][0]["callback_data"]
    assert str(cb["message"]["message_id"]) == sent.message_id

    await _call("/api/telegram/send", {"user": "7100000001", "media_b64": "aGVsbG8=", "filename": "n.pdf"})
    file_id = json.loads(captured[-1].content)["message"]["document"]["file_id"]
    assert await platform.download_file(creds={"bot_token": token}, url=file_id, max_bytes=100) == b"hello"
