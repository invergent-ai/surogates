"""Discord through the shared channel code: memory boundary, reply threading,
writing first into a DM."""

from __future__ import annotations

import dataclasses

import httpx
import respx

from surogates.channels.inbound import ChannelInboundPipeline
from surogates.channels.memory_boundary import boundary_token
from surogates.channels.platforms.discord import DISCORD_API_BASE
from surogates.programs.opener import resolve_dm

from tests.test_channel_pipeline import _make_config, _make_deps, _make_msg, _Routing
from tests.test_channel_pipeline_telegram import _ConfigStore


def test_discord_conversations_never_share_memory():
    b = dict(source={}, fallback_id="f")
    assert boundary_token(platform="discord", channel_id="9", visibility="dm", **b) == "discord:d:9"
    assert boundary_token(platform="discord", channel_id="9", visibility="public", **b) == "discord:c:9"
    assert boundary_token(platform="discord", channel_id="", visibility="dm", **b) == "discord:iso:f"


async def test_a_server_answer_replies_to_the_latest_message_by_default():
    deps = _make_deps()
    deps.session_store = store = _ConfigStore()
    msg = dataclasses.replace(
        _make_msg(text="hi", ts="discord:31", is_dm=False, is_mention=True),
        source={"message_id": "31"},
    )
    await ChannelInboundPipeline().handle(
        msg, routing=_Routing(platform="discord", identifier="app"),
        config=_make_config(require_mention=True), deps=deps,
    )
    assert store.config_writes == [("discord_reply_to_message_id", "31")]


@respx.mock
async def test_writing_first_opens_the_dm_and_keys_the_session_on_it():
    route = respx.post(f"{DISCORD_API_BASE}/users/@me/channels").mock(
        return_value=httpx.Response(200, json={"id": "dm42", "type": 1}),
    )

    async def _creds(kind, identifier, org_id):
        assert (kind, identifier) == ("discord", "app1")
        return {"bot_token": "tok"}

    target = await resolve_dm("discord", "u7", "app1", credentials_for=_creds, org_id="o")
    assert target.session_key == "agent:discord:dm:dm42"
    assert target.destination == {"channel_id": "dm42", "channel_identifier": "app1"}
    assert route.calls[0].request.content == b'{"recipient_id":"u7"}'
