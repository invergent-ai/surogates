"""Per-call settings come from the ops routing row and survive whatever is in it."""
from surogates.voice.agent import GREETING_DEFAULT, CallConfig


def test_call_config_reads_the_routing_row():
    cfg = CallConfig.from_routing({"greeting": "Salut, sunt Ana.", "voice": "male", "remember_callers": True,
                                   "pronunciations": {"Nvidia": "Envidia"}, "max_call_seconds": 300})
    assert (cfg.greeting, cfg.voice, cfg.remember_callers, dict(cfg.pronunciations), cfg.max_call_seconds) == \
        ("Salut, sunt Ana.", "male", True, {"Nvidia": "Envidia"}, 300.0)


def test_call_config_survives_bad_values():
    cfg = CallConfig.from_routing({"greeting": "   ", "voice": "robot", "remember_callers": "yes",
                                   "pronunciations": ["Nvidia"], "max_call_seconds": 10 ** 9,
                                   "idle_ask_seconds": True, "unknown": object()})
    assert cfg == CallConfig()
    assert CallConfig.from_routing(None) == CallConfig() and CallConfig().greeting == GREETING_DEFAULT


async def test_our_own_voice_coming_back_never_reaches_turn_taking(monkeypatch):
    """Speakerphone echo must be dropped before LiveKit counts its words as an interruption."""
    from types import SimpleNamespace

    from livekit.agents import Agent, stt

    from surogates.voice.agent import VoiceAgent

    def said(text):
        return stt.SpeechEvent(type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                               alternatives=[stt.SpeechData(language="ro", text=text)])

    heard = [stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH), said("întrebări legate de știrile zilei"),
             said("cât e euro azi")]

    async def default_stt_node(agent, audio, model_settings):
        for ev in heard:
            yield ev

    monkeypatch.setattr(Agent.default, "stt_node", default_stt_node)
    monkeypatch.setattr(VoiceAgent, "session", property(lambda self: SimpleNamespace(agent_state="speaking")))
    agent = VoiceAgent(CallConfig())
    agent.recent = [(__import__("time").monotonic(), "Te pot ajuta cu întrebări legate de știrile zilei.")]
    out = [ev async for ev in agent.stt_node(None, None)]
    assert [ev.alternatives[0].text if ev.alternatives else ev.type for ev in out] == \
        [stt.SpeechEventType.START_OF_SPEECH, "cât e euro azi"]


def test_only_text_pronunciations_are_spoken():
    cfg = CallConfig.from_routing({"pronunciations": {"Nvidia": None, "DAX": "Dax", "BET": 5, "": "x"}})
    assert dict(cfg.pronunciations) == {"DAX": "Dax"}  # None would have been said aloud as "None"
