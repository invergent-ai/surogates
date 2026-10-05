"""Who a call's session belongs to, and whose memory it may use. The store-backed turn tests are in
tests/integration/test_voice_call_session.py (the session store's SQL is Postgres-only)."""
from uuid import uuid4

import pytest

from surogates.voice import sessions as voice_sessions
from surogates.voice.sessions import CallTarget, VoiceSessions, normalize_caller


@pytest.mark.parametrize("raw, expected", [("+40722000111", "40722000111"), ("40722000111", "40722000111"),
                                           ("", "anonymous"), (None, "anonymous"), ("anonymous", "anonymous")])
def test_normalize_caller(raw, expected):
    assert normalize_caller(raw) == expected


async def _open(monkeypatch, *, caller, remember):
    seen = {}

    async def identity(sf, *, platform, platform_user_id, org_id, display_name=""):
        seen["identity"] = (platform, platform_user_id)
        return type("Ident", (), {"user_id": uuid4()})()

    async def session(store, redis, *, session_key, config, channel, **kw):
        seen["session"] = (session_key, channel, config)
        return uuid4()

    monkeypatch.setattr(voice_sessions, "get_or_create_channel_identity", identity)
    monkeypatch.setattr(voice_sessions, "get_or_create_channel_session", session)
    vs = VoiceSessions(store=None, redis=None, session_factory=None)
    target = CallTarget(org_id=uuid4(), agent_id="agent-1", remember_callers=remember)
    call = await vs.open_call(target, call_id="SCL_1", called="+40300000001", caller=caller, greeting="Bună ziua!")
    return call, seen


async def test_open_call_isolates_each_call_by_default(monkeypatch):
    call, seen = await _open(monkeypatch, caller="+40722000111", remember=False)
    key, channel, config = seen["session"]
    assert (key, channel, config["memory_boundary"]) == ("agent:voice:call:SCL_1", "voice", "voice:call:SCL_1")
    assert seen["identity"] == ("voice", "40722000111")


async def test_open_call_remembers_a_known_number_when_the_agent_asks(monkeypatch):
    _, seen = await _open(monkeypatch, caller="+40722000111", remember=True)
    assert seen["session"][2]["memory_boundary"] == "phone:40722000111"


async def test_open_call_withheld_number_never_remembers(monkeypatch):
    call, seen = await _open(monkeypatch, caller=None, remember=True)
    assert seen["identity"] == ("voice", "anonymous")
    assert seen["session"][2]["memory_boundary"] == "voice:call:SCL_1"
    assert call.caller == "anonymous"
