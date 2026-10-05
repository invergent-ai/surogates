"""A voice session is spoken to, and its memory stays inside the boundary the call chose."""
from types import SimpleNamespace

from surogates.channels.memory_boundary import session_memory_boundary
from surogates.harness.prompt_library import default_library


def test_voice_sessions_get_a_spoken_platform_hint():
    hint = default_library().platform_hint("voice")
    assert hint and "telefon" in hint.lower() and "markdown" in hint.lower()


def test_a_voice_call_keeps_its_own_memory_boundary():
    call = SimpleNamespace(channel="voice", config={"memory_boundary": "voice:call:SCL_1"}, id="s1")
    assert session_memory_boundary(call) == "voice:call:SCL_1"
    remembered = SimpleNamespace(channel="voice", config={"memory_boundary": "phone:40722000111"}, id="s2")
    assert session_memory_boundary(remembered) == "phone:40722000111"
