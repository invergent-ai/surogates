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
