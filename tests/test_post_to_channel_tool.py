"""post_to_channel against a stand-in Slack Web API (the real slack_sdk client)."""

from __future__ import annotations

import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import uvicorn
from fastapi import FastAPI, Request

from surogates.channels.platforms import slack
from surogates.tools.builtin.channel_post import _post_to_channel_handler

calls: list[tuple[str, dict]] = []
app = FastAPI()


@app.api_route("/api/{method}", methods=["GET", "POST"])
async def _slack(method: str, request: Request):
    raw = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        args = json.loads(raw or b"{}")
    else:
        args = {k: v[0] for k, v in parse_qs(raw.decode()).items()} | dict(request.query_params)
    calls.append((method, args))
    if method == "conversations.list":
        if not args.get("cursor"):
            return {"ok": True, "channels": [{"id": "C0RANDOM1", "name": "random"}],
                    "response_metadata": {"next_cursor": "p2"}}
        return {"ok": True, "channels": [{"id": "C0GROWTH1", "name": "growth"},
                                         {"id": "C0SECRET1", "name": "secret"}],
                "response_metadata": {"next_cursor": ""}}
    if method == "users.list":
        return {"ok": True, "members": [
            {"id": "U0MADALIN", "name": "madalin", "profile": {"real_name": "Mădălin Tatarciuc", "display_name": "Madalin"}},
            {"id": "U0GONE", "name": "old", "deleted": True, "profile": {"real_name": "Madalin Tatarciuc"}},
            {"id": "U0BOT", "name": "bridge", "is_bot": True, "profile": {"real_name": "Madalin Tatarciuc"}}],
            "response_metadata": {"next_cursor": ""}}
    if method == "chat.postMessage":
        if args["channel"] == "C0SECRET1":
            return {"ok": False, "error": "not_in_channel"}
        return {"ok": True, "channel": args["channel"], "ts": "1700000000.000100"}
    if method == "chat.getPermalink":
        return {"ok": True, "permalink": f"https://x.slack.com/archives/{args['channel']}/p1700000000000100"}
    if method == "auth.test":
        return {"ok": True, "user": "bridge", "user_id": "UBOT"}
    return {"ok": False, "error": "unknown_method"}


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/api/"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture(autouse=True)
def _fake_slack(server, monkeypatch):
    monkeypatch.setattr(slack, "SLACK_API_BASE", server)
    calls.clear()


class _Vault:
    async def resolve_ref(self, ref, *, org_id):
        return "xoxb-test" if "A0APP" in ref else None


class _Platform:
    def __init__(self, routings):
        self.routings = routings

    async def list_channel_routings(self, kind):
        return self.routings if kind == "slack" else []


def _kwargs(*, routings=None, channel="schedule", config=None):
    if routings is None:
        routings = [{"agent_id": "agent-2", "org_id": "org-1", "channel_identifier": "A0OTHER"},
                    {"agent_id": "agent-1", "org_id": "org-1", "channel_identifier": "A0APP"}]
    # What tool_exec's dispatch passes: the session's parts, never the session itself.
    return {"agent_id": "agent-1", "tenant": SimpleNamespace(org_id="org-1"), "channel": channel,
            "session_config": config or {}, "credential_vault": _Vault(), "platform_client": _Platform(routings)}


async def test_posts_text_blocks_and_image_by_channel_name():
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Growth, daily"}},
              {"type": "section", "text": {"type": "mrkdwn", "text": "```\nsignups  12\n```"}}]
    out = json.loads(await _post_to_channel_handler(
        {"channel": "#growth", "text": "Growth, daily", "blocks": blocks,
         "image_url": "https://example.com/chart.png"}, **_kwargs()))
    assert out == {"success": True, "channel": "C0GROWTH1", "ts": "1700000000.000100",
                   "permalink": "https://x.slack.com/archives/C0GROWTH1/p1700000000000100"}
    assert [m for m, _ in calls] == ["conversations.list", "conversations.list",
                                     "chat.postMessage", "chat.getPermalink"]
    posted = calls[2][1]
    sent_blocks = json.loads(posted["blocks"]) if isinstance(posted["blocks"], str) else posted["blocks"]
    assert sent_blocks[:2] == blocks
    assert sent_blocks[2] == {"type": "image", "image_url": "https://example.com/chart.png", "alt_text": "image"}
    assert posted["text"] == "Growth, daily"


async def test_channel_id_skips_lookup_and_image_alone_keeps_text():
    out = json.loads(await _post_to_channel_handler(
        {"channel": "C0GROWTH1", "text": "hello", "image_url": "https://example.com/a.png"}, **_kwargs()))
    assert out["success"] and out["channel"] == "C0GROWTH1"
    assert calls[0][0] == "chat.postMessage"
    raw = calls[0][1]["blocks"]
    sent = json.loads(raw) if isinstance(raw, str) else raw
    assert sent[0]["text"]["text"] == "hello" and sent[1]["type"] == "image"


async def test_not_in_channel_says_how_to_invite():
    out = json.loads(await _post_to_channel_handler({"channel": "#secret", "text": "hi"}, **_kwargs()))
    assert out["success"] is False
    assert "/invite @bridge" in out["error"] and "#secret" in out["error"]


async def test_agent_without_slack_is_refused_before_any_call():
    out = json.loads(await _post_to_channel_handler(
        {"channel": "#growth", "text": "hi"},
        **_kwargs(routings=[{"agent_id": "agent-2", "org_id": "org-1", "channel_identifier": "A0OTHER"}])))
    assert out["success"] is False and "not connected to Slack" in out["error"]
    assert calls == []


async def test_slack_session_uses_its_own_app():
    out = json.loads(await _post_to_channel_handler(
        {"channel": "C0GROWTH1", "text": "hi"},
        **_kwargs(routings=[], channel="slack", config={"channel_identifier": "A0APP"})))
    assert out["success"] is True


async def test_input_limits():
    too_many = [{"type": "divider"}] * 51
    out = json.loads(await _post_to_channel_handler(
        {"channel": "#growth", "text": "hi", "blocks": too_many}, **_kwargs()))
    assert "at most 50" in out["error"]
    out = json.loads(await _post_to_channel_handler(
        {"channel": "#growth", "text": "hi", "image_url": "http://insecure/a.png"}, **_kwargs()))
    assert "https" in out["error"]
    assert calls == []


async def test_a_person_id_posts_in_their_dm():
    out = json.loads(await _post_to_channel_handler({"channel": "U0PERSON1", "text": "a question for you"}, **_kwargs()))
    assert out["success"] and out["channel"] == "U0PERSON1"
    assert calls[0][0] == "chat.postMessage" and calls[0][1]["channel"] == "U0PERSON1"


async def test_a_person_by_name_gets_a_dm():
    out = json.loads(await _post_to_channel_handler({"channel": "@Madalin Tatarciuc", "text": "Confirm @surogate_ai?"}, **_kwargs()))
    assert out["success"] and out["channel"] == "U0MADALIN"
    out = json.loads(await _post_to_channel_handler({"channel": "@Nobody Here", "text": "x"}, **_kwargs()))
    assert not out["success"] and "No one in Slack" in out["error"]
