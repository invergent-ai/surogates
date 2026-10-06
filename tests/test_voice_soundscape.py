"""The call's background sound: a faint room, rare distant events, and sounds caused by what the agent does."""
import numpy as np
import pytest

from surogates.voice.soundscape import Pack, SoundSettings, Soundscape, render_offline
from surogates.voice.text import asks_for_details

RATE = 8000
FRAME = RATE // 50  # 20 ms


def tone(seconds: float, freq: float, level_dbfs: float = -20.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    x = np.sin(2 * np.pi * freq * t).astype(np.float32)
    return x / np.sqrt(np.mean(x ** 2)) * 10 ** (level_dbfs / 20)


def pack() -> Pack:
    """Every clip a distinct tone, so a test can tell which one is playing."""
    return Pack(rate=RATE, level_dbfs=-20.0,
                beds={"clinic": tone(3.0, 100)},
                events={"clinic": [[tone(0.5, 200), tone(0.5, 210)], [tone(0.5, 220)]]},
                actions={"keys": [tone(0.3, 300 + 10 * i) for i in range(4)], "keys-enter": [tone(0.3, 400)],
                         "click": [tone(0.1, 500), tone(0.1, 510)], "pen": [tone(0.6, 600)],
                         "page": [tone(0.3, 700)], "chair": [tone(0.3, 800)]},
                hold={"piano": tone(2.0, 900)})


def scape(seed: int = 1, **kw) -> Soundscape:
    return Soundscape(pack(), SoundSettings(**{"scene": "clinic", **kw}), voice_dbfs=-20.0, seed=seed)


def run(s: Soundscape, seconds: float) -> np.ndarray:
    return np.concatenate([s.frame() for _ in range(int(seconds * 50))])


def db(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)


# --- settings -------------------------------------------------------------------------------

def test_settings_fall_back_to_defaults_on_bad_values():
    s = SoundSettings.from_routing({"scene": "clinic", "room": "loud", "events": 3, "actions": "yes", "hold": 7})
    assert s == SoundSettings(scene="clinic", room="normal", events="rare", actions=True, hold=None)
    assert SoundSettings.from_routing(None).silent
    assert SoundSettings.from_routing({"scene": ""}).silent


def test_details_question_is_recognized():
    assert asks_for_details("Perfect. Pe ce nume fac programarea?")
    assert asks_for_details("Îmi puteți da un număr de telefon?")
    assert asks_for_details("Care este adresa de email?")
    assert not asks_for_details("Vă pot ajuta cu altceva?")
    assert not asks_for_details("Am notat numele.")


# --- the room ---------------------------------------------------------------------------------

def test_silent_scene_and_everything_off_are_digital_silence():
    s = Soundscape(pack(), SoundSettings(scene=None), voice_dbfs=-20.0, seed=1)
    s.agent_thinking(True)
    assert not run(s, 3).any()
    s = scape(room="off", events="off", actions=False)
    s.agent_thinking(True)
    assert not run(s, 3).any()


def test_room_is_louder_behind_the_voice_and_low_is_quieter():
    s = scape(events="off")
    quiet = run(s, 2)[RATE:]
    s.agent_speaking(True)
    talking = run(s, 2)[RATE:]
    assert db(quiet) == pytest.approx(-20 - 44, abs=1.5)
    assert db(talking) == pytest.approx(-20 - 34, abs=1.5)
    low = scape(events="off", room="low")
    assert db(run(low, 2)[RATE:]) == pytest.approx(-20 - 50, abs=1.5)


def test_room_fades_in_at_pickup_and_out_at_the_end():
    s = scape(events="off")
    first = s.frame()
    assert np.abs(first).max() < 0.2 * np.abs(run(s, 1)).max()
    s.end()
    run(s, 1)
    assert not run(s, 0.5).any()


# --- actions ------------------------------------------------------------------------------------

def test_checking_starts_only_after_a_moment_of_thinking():
    s = scape(room="off", events="off")
    s.agent_thinking(True)
    assert not run(s, 0.7).any()  # a quick reply gets no sound at all
    assert run(s, 1.5).any()


def test_checking_stops_when_the_agent_speaks():
    s = scape(room="off", events="off")
    s.agent_thinking(True)
    run(s, 2)
    s.agent_speaking(True)
    run(s, 0.1)
    assert not run(s, 2).any()


def test_caller_interrupting_cuts_actions_at_once():
    s = scape(room="off", events="off")
    s.agent_thinking(True)
    out = run(s, 2.5)
    assert out.any()
    s.caller_speaking(True)
    assert not np.concatenate([s.frame() for _ in range(4)])[-FRAME:].any()  # silent within 80 ms
    assert not run(s, 2).any()


def test_no_variant_plays_twice_in_a_row():
    s = scape(room="off", events="off", seed=3)
    picks = [s._pick("keys") for _ in range(50)]
    assert all(a is not b for a, b in zip(picks, picks[1:]))


def test_writing_plays_a_pen_shortly_after():
    s = scape(room="off", events="off")
    s.writing()
    assert not run(s, 0.7).any()
    assert run(s, 1).any()


def test_actions_off_means_no_typing_and_no_pen():
    s = scape(room="off", events="off", actions=False)
    s.agent_thinking(True)
    s.writing()
    assert not run(s, 4).any()


# --- hold -----------------------------------------------------------------------------------------

def test_hold_music_only_after_a_long_wait_and_only_when_chosen():
    s = scape(room="off", events="off", actions=False, hold="piano")
    s.agent_thinking(True)
    assert not run(s, 8.5).any()
    assert run(s, 2).any()
    s.agent_speaking(True)
    run(s, 1)
    assert not run(s, 1).any()
    none = scape(room="off", events="off", actions=False)
    none.agent_thinking(True)
    assert not run(none, 12).any()


# --- events ---------------------------------------------------------------------------------------

def test_events_never_start_right_after_the_agent_starts_speaking():
    s = scape(room="off", actions=False, events="normal", seed=5)
    starts = []
    for i in range(int(120 * 50)):
        if i % 150 == 0:
            s.agent_speaking(True)
        elif i % 150 == 100:
            s.agent_speaking(False)
        before = len(s._voices)
        s.frame()
        if len(s._voices) > before:
            starts.append(i % 150)
    assert starts, "events should happen in two minutes at the normal rate"
    assert all(k >= 30 for k in starts)  # never in the first 0.6 s of a phrase


def test_events_rate_follows_the_setting():
    def count(rate: str) -> int:
        s = scape(room="off", actions=False, events=rate, seed=9)
        n = 0
        for _ in range(int(600 * 50)):
            before = len(s._voices)
            s.frame()
            n += len(s._voices) > before
        return n
    rare, normal = count("rare"), count("normal")
    assert 10 <= rare <= 40 and 25 <= normal <= 70 and normal > rare


# --- offline rendering (Studio previews) ---------------------------------------------------------

def test_render_offline_applies_cues_at_their_times():
    s = scape(room="off", events="off")
    out = render_offline(s, [(1.0, "agent_thinking", True), (3.0, "agent_speaking", True)], seconds=5)
    assert len(out) == 5 * RATE
    assert not out[: int(1.7 * RATE)].any()
    assert out[int(2.0 * RATE): int(3.0 * RATE)].any()
    assert not out[int(3.2 * RATE):].any()


# --- the sound pack ------------------------------------------------------------------------------

MANIFEST = {
    "level_dbfs": -20,
    "actions": {"keys": ["keys-a.ogg"], "pen": ["pen-a.ogg"]},
    "scenes": [{"id": "clinic", "bed": "bed-clinic.ogg", "events": {"door": ["door-a.ogg", "door-b.ogg"]}},
               {"id": "bank", "bed": "bed-bank.ogg", "events": {}}],
    "hold": [{"id": "piano", "file": "hold-piano.ogg"}],
}


def fake_server(manifest=MANIFEST, fail=False):
    import json as _json
    asked = []

    async def get(name):
        asked.append(name)
        if fail:
            raise ConnectionError("ops is down")
        return _json.dumps(manifest).encode() if name == "manifest.json" else b"audio:" + name.encode()
    return get, asked


@pytest.mark.asyncio
async def test_fetch_pack_downloads_only_what_the_call_uses_and_caches_it(tmp_path):
    from surogates.voice.soundscape import fetch_pack
    get, asked = fake_server()
    settings = SoundSettings(scene="clinic", actions=False)
    assert await fetch_pack(get, tmp_path, settings) == tmp_path
    assert sorted(asked) == ["bed-clinic.ogg", "door-a.ogg", "door-b.ogg", "manifest.json"]
    asked.clear()
    await fetch_pack(get, tmp_path, settings)
    assert asked == ["manifest.json"]  # the clips are cached


@pytest.mark.asyncio
async def test_fetch_pack_gives_up_quietly(tmp_path):
    from surogates.voice.soundscape import fetch_pack
    get, _ = fake_server(fail=True)
    assert await fetch_pack(get, tmp_path, SoundSettings(scene="clinic")) is None
    bad = {**MANIFEST, "scenes": [{"id": "clinic", "bed": "../../etc/passwd.ogg", "events": {}}]}
    get, _ = fake_server(bad)
    assert await fetch_pack(get, tmp_path / "x", SoundSettings(scene="clinic", actions=False)) is None
    assert not (tmp_path.parent / "etc").exists()
    get, asked = fake_server()
    assert await fetch_pack(get, tmp_path, SoundSettings(scene=None)) is None
    assert asked == []


def test_pack_load_reads_only_the_chosen_scene(tmp_path):
    import json as _json
    (tmp_path / "manifest.json").write_text(_json.dumps(MANIFEST))
    loaded = []
    p = Pack.load(tmp_path, SoundSettings(scene="clinic", hold="piano"), rate=RATE,
                  decode=lambda path, rate: loaded.append(path.name) or tone(0.1, 100))
    assert set(p.beds) == {"clinic"} and len(p.events["clinic"]) == 1 and len(p.events["clinic"][0]) == 2
    assert set(p.actions) == {"keys", "pen"} and set(p.hold) == {"piano"}
    assert "bed-bank.ogg" not in loaded


# --- cleanup (found by voice-qa: a crash here left every call's line taken) ----------------------

def test_overlapping_sounds_of_different_lengths_finish_without_error():
    s = scape(room="off", events="off")
    s._play("event", tone(2.0, 200), -30)
    s._play("action", tone(0.3, 300), -19)  # finishes first and is removed while the longer one plays
    run(s, 3)
    assert s._voices == []


@pytest.mark.asyncio
async def test_closing_the_player_never_hangs_on_a_room_that_is_gone():
    import asyncio
    from types import SimpleNamespace
    from surogates.voice.soundscape import SoundscapePlayer

    async def forever(_sid):
        await asyncio.Event().wait()

    player = SoundscapePlayer(scape())
    player._task = asyncio.ensure_future(asyncio.Event().wait())
    player._track = SimpleNamespace(sid="TR_1")
    player._room = SimpleNamespace(isconnected=lambda: True, local_participant=SimpleNamespace(unpublish_track=forever))
    await asyncio.wait_for(player.aclose(), timeout=4)  # raises TimeoutError if it hangs
    player._task = asyncio.ensure_future(asyncio.Event().wait())
    player._room = SimpleNamespace(isconnected=lambda: False, local_participant=None)  # gone: nothing to unpublish
    await asyncio.wait_for(player.aclose(), timeout=4)


@pytest.mark.asyncio
async def test_two_calls_filling_the_cache_at_once_both_get_their_sounds(tmp_path):
    """Calls start in parallel (one process each, one cache): writing the same temp file raced."""
    import asyncio
    import json as _json
    from surogates.voice.soundscape import fetch_pack

    async def get(name):
        await asyncio.sleep(0)  # interleave the two calls at every download
        return _json.dumps(MANIFEST).encode() if name == "manifest.json" else b"audio:" + name.encode()

    settings = SoundSettings(scene="clinic")
    results = await asyncio.gather(*(fetch_pack(get, tmp_path, settings) for _ in range(2)))
    assert results == [tmp_path, tmp_path]
    assert _json.loads((tmp_path / "manifest.json").read_text())["level_dbfs"] == -20
    assert not list(tmp_path.glob("*.part*"))


def test_a_clip_is_decoded_once_per_pod_not_once_per_call(tmp_path, monkeypatch):
    import surogates.voice.soundscape as sc
    decoded = []

    def fake_decode(path, rate):
        decoded.append(path.name)
        return tone(0.2, 440)

    monkeypatch.setattr(sc, "decode_av", fake_decode)
    clip = tmp_path / "room-clinic-1a2b.ogg"
    clip.write_bytes(b"ogg")
    first = sc.decode_cached(clip, RATE)
    second = sc.decode_cached(clip, RATE)  # the next call, in another process: from the cache
    assert decoded == ["room-clinic-1a2b.ogg"]
    assert np.array_equal(first, second)
