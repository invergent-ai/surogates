import json

from surogates.tools.builtin.channel_messages import _fetch_channel_messages_handler
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


async def test_missing_api_client_returns_error():
    out = json.loads(await _fetch_channel_messages_handler({"limit": 10}))
    assert out["success"] is False


async def test_delegates_with_parsed_args():
    calls = {}

    class _Client:
        async def fetch_channel_messages(self, *, limit, since, user):
            calls.update(limit=limit, since=since, user=user)
            return json.dumps({"success": True, "count": 0})

    out = await _fetch_channel_messages_handler(
        {"limit": "25", "since": "7d", "user": "<@U1>"}, api_client=_Client())
    assert json.loads(out)["success"] is True
    assert calls == {"limit": 25, "since": "7d", "user": "<@U1>"}


async def test_blank_since_and_user_become_none():
    calls = {}

    class _Client:
        async def fetch_channel_messages(self, *, limit, since, user):
            calls.update(limit=limit, since=since, user=user)
            return json.dumps({"success": True})

    await _fetch_channel_messages_handler(
        {"since": "", "user": ""}, api_client=_Client())
    assert calls == {"limit": None, "since": None, "user": None}


def test_tool_routed_to_harness():
    assert TOOL_LOCATIONS["fetch_channel_messages"] == ToolLocation.HARNESS
