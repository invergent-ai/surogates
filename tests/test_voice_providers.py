"""Speech providers: the catalog, building each provider's plugin, and the owner's account calls."""
import json
from uuid import uuid4

import httpx
import numpy as np
import pytest

from surogates.voice.providers import PROVIDERS, SUROGATE, Conn, check, list_voices
from surogates.voice.speech import ShapedTTS, Slot, build_stt, build_tts, turn_handling
from surogates.voice.stt import RoSTT
from surogates.voice.tts import PhraseCache, RoTTS


def _slot(pid: str, **kw) -> Slot:
    extra = {"base_url": "http://speech.local/v1", "model": "my-model"} if pid == "openai_compat" else {}
    return Slot(provider=pid, voice="v1" if pid != SUROGATE else "female", **{**extra, **kw})


@pytest.mark.parametrize("pid", sorted(PROVIDERS))
def test_every_provider_builds_what_the_catalog_says_it_offers(pid):
    """A provider is never half added: each kind it lists builds, with the catalog's model, offline."""
    p = PROVIDERS[pid]
    if p.stt:
        heard = build_stt(_slot(pid), key="k", language="en", stt_url="ws://stt")
        assert heard.model in (p.stt[0].id, "unknown", "my-model")  # gradium does not report its model
    else:
        with pytest.raises(ValueError):
            build_stt(_slot(pid), key="k", language="en", stt_url="ws://stt")
    if p.tts:
        spoken = build_tts(_slot(pid), key="k", language="en", tts_url="http://tts")
        assert isinstance(spoken, RoTTS if pid == SUROGATE else ShapedTTS)
        assert spoken.model == (p.tts[0].id or "my-model") or pid == SUROGATE


def test_retired_plugin_defaults_are_never_used():
    assert build_tts(_slot("cartesia"), key="k", language="ro", tts_url="").model == "sonic-3.6"  # plugin: sonic-3
    assert build_tts(_slot("elevenlabs"), key="k", language="ro", tts_url="").model == "eleven_flash_v2_5"


def test_routing_slots_old_rows_stay_ours_and_bad_ones_too():
    assert Slot.from_routing(None, voice="male") == Slot(voice="male")
    assert Slot.from_routing({"provider": "nobody"}).ours
    assert Slot.from_routing({"provider": SUROGATE, "voice": "robot"}).voice == "female"
    s = Slot.from_routing({"provider": "cartesia", "model": "sonic-3.6", "voice": "abc", "key_ref": "vault://cartesia",
                           "options": {"speed": 1.1}})
    assert (s.provider, s.voice, s.key_ref, dict(s.options)) == ("cartesia", "abc", "vault://cartesia", {"speed": 1.1})


def test_our_stt_ends_turns_itself_and_providers_wait_for_quiet():
    assert turn_handling(Slot())["turn_detection"] == "stt"
    assert turn_handling(_slot("deepgram"))["turn_detection"] == "vad"
    assert isinstance(build_stt(Slot(), key="", language="ro", stt_url="ws://stt"), RoSTT)


def test_a_providers_cached_phrase_belongs_to_its_org():
    spoken = build_tts(_slot("elevenlabs"), key="k", language="en", tts_url="")
    a, b = PhraseCache(None, spoken, scope=str(uuid4())), PhraseCache(None, spoken, scope=str(uuid4()))
    assert a._key("Hello!") != b._key("Hello!")
    assert PhraseCache(None, RoTTS(url="x"))._key("Bună!").startswith("voice:phrase:surogate::")


async def test_a_provider_voice_is_shaped_like_ours():
    from livekit import rtc
    from livekit.agents import tts, utils

    class Inner(tts.TTS):
        def __init__(self):
            super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=16000, num_channels=1)

        def synthesize(self, text, *, conn_options=None):
            return InnerStream(tts=self, input_text=text, conn_options=conn_options or tts.DEFAULT_API_CONNECT_OPTIONS)

    class InnerStream(tts.ChunkedStream):
        async def _run(self, out):
            out.initialize(request_id=utils.shortuuid(), sample_rate=16000, num_channels=1, mime_type="audio/pcm")
            t = np.arange(16000) / 16000
            out.push((0.1 * np.sin(2 * np.pi * 300 * t) * 32767).astype("<i2").tobytes())
            out.flush()

    shaped = ShapedTTS(Inner(), provider="cartesia", model="m", voice="v")
    async with shaped.synthesize("Hi.") as stream:
        frames = [ev.frame async for ev in stream]
    pcm = np.concatenate([np.frombuffer(f.data, "<i2") for f in frames]).astype(np.float32) / 32768
    assert frames and all(isinstance(f, rtc.AudioFrame) and f.sample_rate == 16000 for f in frames)
    assert np.sqrt(np.mean(pcm ** 2)) > 0.1 / np.sqrt(2)  # lifted, as ours is
    assert np.abs(pcm).max() < 0.98  # never clipped


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_key_check_maps_the_providers_answer():
    for status, code in ((401, "invalid_key"), (402, "no_credits"), (403, "forbidden"), (429, "rate_limited")):
        async with _client(lambda r, s=status: httpx.Response(s, text="no")) as c:
            assert (await check(Conn("cartesia", "k"), c)).code == code
    async with _client(lambda r: httpx.Response(401, text='{"detail":{"status":"quota_exceeded"}}')) as c:
        assert (await check(Conn("elevenlabs", "k"), c)).code == "no_credits"
    async with _client(lambda r: httpx.Response(200, json={"voices": []})) as c:
        assert (await check(Conn("elevenlabs", "k"), c)).ok

    def down(r):
        raise httpx.ConnectError("no route")

    async with _client(down) as c:
        assert (await check(Conn("openai_compat", base_url="http://gone/v1"), c)).code == "unreachable"


async def test_each_providers_voices_come_back_in_one_shape():
    answers = {
        "api.elevenlabs.io": {"voices": [{"voice_id": "e1", "name": "Ana", "category": "cloned",
                                          "labels": {"gender": "female", "accent": "romanian"},
                                          "preview_url": "https://cdn/e1.mp3",
                                          "verified_languages": [{"language": "ro"}]}]},
        "api.cartesia.ai": {"data": [{"id": "c1", "name": "Katie", "language": "en", "gender": "feminine",
                                      "is_owner": False, "preview_file_url": "https://cdn/c1.wav"}]},
        "api.deepgram.com": {"tts": [{"canonical_name": "aura-2-thalia-en", "name": "thalia", "languages": ["en"],
                                      "metadata": {"accent": "American", "sample": "https://cdn/t.wav"}},
                                     {"canonical_name": "aura-2-nestor-es", "name": "nestor", "languages": ["es"],
                                      "metadata": {}}]},
        "api.gradium.ai": [{"uid": "g1", "name": "Emma", "language": "fr", "is_catalog": False}],
        "api.fish.audio": {"items": [{"_id": "f1", "title": "Narrator", "languages": ["en"],
                                      "samples": [{"audio": "https://cdn/f1.mp3"}]}]},
    }
    seen = []

    def handler(r: httpx.Request):
        seen.append(r)
        return httpx.Response(200, content=json.dumps(answers[r.url.host]))

    async with _client(handler) as c:
        _, el = await list_voices(Conn("elevenlabs", "k"), c)
        assert el[0].id == "e1" and el[0].custom and el[0].languages == ("ro",) and el[0].preview_url
        _, ca = await list_voices(Conn("cartesia", "k"), c, library=True)
        assert (ca[0].id, ca[0].preview_url, ca[0].custom) == ("c1", "https://cdn/c1.wav", False)
        _, dg = await list_voices(Conn("deepgram", "k"), c, language="es")
        assert [v.id for v in dg] == ["aura-2-nestor-es"]  # filtered here: Deepgram has no language filter
        _, gr = await list_voices(Conn("gradium", "k"), c, query="emm")
        assert gr[0].id == "g1" and gr[0].custom
        _, fi = await list_voices(Conn("fishaudio", "k"), c)
        assert fi[0].preview_url == "https://cdn/f1.mp3"
    assert seen[0].headers["xi-api-key"] == "k" and "Cartesia-Version" in seen[1].headers
    assert "is_owner" not in seen[1].url.params  # the library is every voice, not the owner's


async def test_fixed_and_typed_voices_need_no_call():
    async with _client(lambda r: pytest.fail("no request expected")) as c:
        _, oa = await list_voices(Conn("openai", "k"), c)
        assert "alloy" in [v.id for v in oa]
        assert (await list_voices(Conn("openai_compat", base_url="http://x/v1"), c))[1] == []


async def test_a_provider_line_without_its_key_refuses_the_call_instead_of_speaking_silence():
    from surogates.voice.worker import ProviderUnavailable, Runtime

    class Vault:
        def __init__(self, value):
            self.value, self.asked = value, []

        async def retrieve(self, org_id, name):
            self.asked.append(name)
            return self.value

    org, slot = uuid4(), Slot(provider="cartesia", voice="v", key_ref="vault://cartesia-key")
    rt = Runtime(engine=None, redis=None, client=None, routing=None, sessions=None, vault=Vault("sk-1"))
    assert await rt.key(org, slot) == "sk-1" and rt.vault.asked == ["cartesia-key"]
    assert await rt.key(org, Slot()) == ""  # ours needs none
    for vault, reason in ((Vault(None), "key_missing"), (None, "no_vault")):
        with pytest.raises(ProviderUnavailable) as e:
            await Runtime(engine=None, redis=None, client=None, routing=None, sessions=None, vault=vault).key(org, slot)
        assert e.value.reason == reason


def test_the_tone_is_short_and_never_clips():
    from surogates.voice.worker import tone

    (frame,) = tone()
    pcm = np.frombuffer(frame.data, "<i2")
    assert 0.5 < len(pcm) / frame.sample_rate < 1.0 and np.abs(pcm).max() < 32767 * 0.3


async def test_a_failing_provider_sentence_is_tried_a_few_times_not_sixteen():
    from livekit.agents import APIConnectionError, APIConnectOptions, tts

    from surogates.voice.speech import ShapedTTS

    tries = 0

    class Down(tts.TTS):
        def __init__(self):
            super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=24000, num_channels=1)

        def synthesize(self, text, *, conn_options=None):
            return DownStream(tts=self, input_text=text, conn_options=conn_options)

    class DownStream(tts.ChunkedStream):
        async def _run(self, output_emitter):
            nonlocal tries
            tries += 1
            raise APIConnectionError("timed out")

    shaped = ShapedTTS(Down(), provider="elevenlabs", model="m", voice="v")
    options = APIConnectOptions(max_retry=3, retry_interval=0.0, timeout=1.0)
    with pytest.raises(APIConnectionError):
        async with shaped.synthesize("Hello.", conn_options=options) as stream:
            async for _ in stream:
                pass
    assert tries == 4  # once and three retries: the wrapper retries, the provider inside does not


def test_a_cached_phrase_is_redone_when_anything_that_shapes_its_audio_changes():
    from dataclasses import replace

    from surogates.voice.speech import phrase_scope

    org, el = str(uuid4()), _slot("elevenlabs")
    base = phrase_scope(el, "en", org)
    assert base.startswith(org) and phrase_scope(el, "en", org) == base
    assert phrase_scope(replace(el, options={"speed": 1.1}), "en", org) != base
    assert phrase_scope(el, "de", org) != base
    compat = _slot("openai_compat")
    assert phrase_scope(compat, "en", org) != phrase_scope(replace(compat, base_url="http://other/v1"), "en", org)
    assert phrase_scope(Slot(), "ro", org) == ""  # our voices are shared by every line


async def test_a_voice_list_the_provider_garbled_is_a_status_not_a_crash():
    from surogates.voice.providers import list_voices

    answers = [("elevenlabs", httpx.Response(200, text="<html>proxy login</html>")),  # not JSON
               ("elevenlabs", httpx.Response(200, json={"voices": [{"name": "no id"}]})),  # a voice without its id
               ("fishaudio", httpx.Response(200, json={"items": "?"}))]  # not a list
    for pid, answer in answers:
        async with _client(lambda r, a=answer: a) as c:
            status, voices = await list_voices(Conn(pid, "k"), c)
        assert not status.ok and status.code == "error" and voices == []
