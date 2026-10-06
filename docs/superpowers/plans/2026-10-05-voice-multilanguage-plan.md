# Voice: other languages and premium voices — plan (later)

> Not scheduled. The voice channel ships first with our own Romanian models on CPU. This records
> the design agreed on 2026-10-05 so it can be built without re-deciding it. Provider facts,
> prices and licensing: `surogate-ops/docs/voice-providers.md` (private).

**Goal:** an agent's phone line can speak any language and offer premium voices, while Romanian
on our own models stays the default and included in the price.

## Design

**Voice profile per agent** (routing config, set in Studio):

| key | values | default |
|---|---|---|
| `language` | ISO 639-1 (`ro`, `en`, `de`, …) | `ro` |
| `voice` | `surogate:female`, `surogate:male`, `elevenlabs:<voice_id>` | `surogate:female` (today `female`) |
| `voice_model` | ElevenLabs model id | `eleven_flash_v2_5` |

| language | STT | TTS | end of turn |
|---|---|---|---|
| `ro` | RoSTT (ours) | RoTTS (ours) or ElevenLabs | STT pause (today) |
| other | ElevenLabs Scribe realtime | ElevenLabs | Scribe server VAD commit → `turn_detection="stt"` as today |

- **Plugins:** `livekit-plugins-elevenlabs` 1.8.x (TTS over the multi-context websocket, STT
  `scribe_v2_realtime`). Encoding `pcm_24000` (the plugin has no ulaw); LiveKit SIP transcodes.
- **Per-language text:** the Romanian layer (`say_as`, abbreviations, preamble and goodbye
  patterns) applies to `ro` only; other languages get `clean()` and a generic splitter. Fixed
  phrases (greeting default, "still there?", goodbye, sorry, busy, unavailable, the tool filler)
  become a per-language table with an English fallback. `platforms/voice.md` becomes
  language-neutral and the session `system` says which language the call is in.
- **Phrase cache** key already includes the TTS model and voice; it works unchanged.
- **Keys:** one ElevenLabs service-account API key per customer (monthly character cap, usage by
  key), stored in the credential vault; the platform key only lists voices.
- **Metering:** the worker adds `tts_chars` (from `character-cost`, or counted when the websocket
  does not return it) and `stt_seconds` to the call's usage report; ops converts them to media
  credits at cost plus margin.
- **Studio:** language select; voice select grouped "Surogate" / "ElevenLabs" (ops lists the
  account's voices, cached); a preview button.
- **Tests to add:** a scenario per language (English call through Scribe + ElevenLabs), a premium
  Romanian voice scenario, provider-down fallback to our Romanian voice with a log line.

## Preconditions (business)

1. ElevenLabs **Scale** plan (OEM terms) before selling their voices; end-user terms flowed down.
2. EU customers needing zero retention or EU residency → ElevenLabs Enterprise.
3. Prices to customers: premium per-minute surcharge decided in ops pricing.
