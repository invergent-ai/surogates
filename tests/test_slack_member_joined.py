from types import SimpleNamespace
from surogates.channels.platforms.slack import SlackPlatform

def _body(event_type, channel="C1", user="U_BOT"):
    return {"type": "event_callback",
            "event": {"type": event_type, "channel": channel, "user": user}}

def _routing():
    return SimpleNamespace(org_id="o1", agent_id="a1", identifier="A0X", config={})

async def test_bot_join_warms_cache_and_acks():
    p = SlackPlatform()
    p._resolve_bot_user_id = lambda token: _async("U_BOT")           # type: ignore
    warmed = {}
    async def fake_warm(**kw): warmed.update(kw); return True
    p._warm_cache = fake_warm  # injected hook (see impl)                # type: ignore
    deps = SimpleNamespace(redis=object())
    handled = await p.handle_non_message_update(
        _body("member_joined_channel", user="U_BOT"),
        routing=_routing(), creds={"bot_token": "x"}, deps=deps)
    assert handled is True
    assert warmed["channel_id"] == "C1"

async def test_other_user_join_is_ignored():
    p = SlackPlatform()
    p._resolve_bot_user_id = lambda token: _async("U_BOT")            # type: ignore
    deps = SimpleNamespace(redis=object())
    handled = await p.handle_non_message_update(
        _body("member_joined_channel", user="U_HUMAN"),
        routing=_routing(), creds={"bot_token": "x"}, deps=deps)
    assert handled is False  # not our bot → fall through, do not ACK-swallow

async def test_non_join_event_falls_through():
    p = SlackPlatform()
    deps = SimpleNamespace(redis=object())
    handled = await p.handle_non_message_update(
        _body("reaction_added"), routing=_routing(), creds={"bot_token": "x"}, deps=deps)
    assert handled is False

def _async(value):
    async def _coro(*a, **k): return value
    return _coro()
