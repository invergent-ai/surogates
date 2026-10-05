# Voice channel (phone calls over SIP) — design

**Date:** 2026-10-05
**Status:** approved, ready for planning
**Repos touched:** `surogates` (the `surogates voice` process, session plumbing),
`surogate-ops` (phone-number pool, Voice channel in Studio, billing), prod infra
(LiveKit server + SIP on `the prod node that holds the public IP`)

Adds `voice` as a channel: an agent answers phone calls on a Romanian number we
own. Calls arrive over a SIP trunk from our carrier, LiveKit turns each call
into a room, and a new `surogates voice` process runs the conversation —
Romanian speech in, the agent's session in the middle, Romanian speech out.

It replaces an earlier Twilio prototype (one hard-coded agent). The prototype's
Romanian handling is kept; its plumbing is not.

---

## 1. Product decisions

| # | Decision | Consequence |
|---|---|---|
| 1 | **Our own SIP trunk, not Twilio.** An IP-based carrier trunk, G.711, DTMF RFC 4733. Carrier, terms and addresses are in the private ops runbook. | We run the SIP side ourselves (LiveKit SIP). The trunk is bound to one public IP for both directions, so SIP runs with host networking on the node that holds it. |
| 2 | **LiveKit, not Asterisk + our bridge.** | Turn-taking, interruptions, false-interruption recovery, transfers, outbound calls, recording and browser voice come with the framework. We write two small plugins (STT, TTS) and the session glue. |
| 3 | **A pool of numbers we own; an agent picks one.** One number belongs to at most one agent. | Ops gets a `phone_numbers` table; the claim reuses `channel_routing`'s unique `(kind, identifier)`. |
| 4 | **Voice lives inside surogates** as `surogates voice`, using the runtime's internal session APIs — not as a client of `/v1/api/chat/completions`. | Real turn ids, real interruption (pause), tool events for the "typing" sound, `channel="voice"` sessions visible in Studio, no per-agent API keys. |
| 5 | **Remembering callers is a per-agent toggle, off by default.** | Caller ID can be spoofed, so a phone number is never proof of identity. Off: every call is a fresh, isolated session with no shared memory. On: memory scoped to `phone:<E.164>`. |
| 6 | **Our own Romanian models on CPU** (`surogate serve` STT jackrabbit-110m-ro-streaming, TTS amami-110m-ro), no GPU for voice in v1. | Latency comes from trimming, caching and harness work, not hardware. Capacity is bounded by STT streams (8 per pod). |
| 7 | **Development uses the prod speech services** through an SSH tunnel (private runbook). | No local speech models on the 8 GB dev laptop. |

---

## 2. Architecture

```
caller ─SIP/RTP─► livekit-sip (host network, on the node holding the trunk's IP)
                     │  one room per call: call-<uuid>
                     ▼
                  livekit-server (internal) ──dispatch "surogate-voice"──┐
                                                                        ▼
                     surogates voice  (N replicas; one job = one call, own process)
       ┌──────────────────┼─────────────────────────────┬──────────────────┐
  RoSTT plugin       SurogatesLLM plugin            RoTTS plugin       ops routing
  ws /v1/audio/      sessions + queue + pub/sub     POST /v1/audio/    voice:<DID> →
  streams (16 kHz)   (internal, like `channels`)    speech (pcm)       agent + settings
```

- **One dispatch rule** for all numbers. The worker reads the SIP participant
  attributes: `sip.trunkPhoneNumber` (our number that was called) and
  `sip.phoneNumber` (the caller, may be anonymous).
- **Routing** reuses ops `channel_routing` with `kind="voice"`,
  `identifier="+40…"`, fetched like every other channel
  (`/api/channels/by-identifier/voice/<number>`, 30 s cache, invalidated on
  `channel_routing_changed:`).
- **The agent is the LLM.** `SurogatesLLM` implements LiveKit's `llm.LLM`:
  each `chat()` posts the caller's utterance as a user message into the call's
  session, enqueues the turn, and streams the turn's `llm.delta` text back as
  `ChatChunk`s. Tools run in the harness as usual; LiveKit never sees them.

---

## 3. A call, end to end

1. **Admission.** The job is accepted only while the worker is under its call
   limit and a global Redis counter of STT streams is under capacity. Over
   capacity: answer, play the cached "toate liniile sunt ocupate", hang up.
2. **Resolve.** Called number → routing row → `{org_id, agent_id, config}`.
   No row (number not assigned): play the cached "acest număr nu este
   disponibil" and hang up.
3. **Greet at once.** The greeting is synthesized once per (text, voice) and
   cached in Redis as PCM; it plays the moment the caller is connected.
   **While it plays,** the session is created so the first real turn starts warm.
4. **Each turn.** STT final (640 ms pause) → `SurogatesLLM` posts the text →
   deltas stream back → the Romanian text layer → TTS → silence trimmed → caller.
5. **Tools.** The agent says its own short line first ("O clipă, verific…").
   On a tool-start event the worker starts the keyboard-typing sound
   (`BackgroundAudioPlayer`), and stops it when speech resumes.
6. **Barge-in.** LiveKit cuts the audio. The worker **pauses** the turn in
   surogates and records **what the caller actually heard** as the assistant
   reply, so history is true and no stale tail leaks into the next answer.
7. **Two breaths.** If the caller speaks again before hearing any of the answer,
   the next message carries both halves as one question.
8. **Hang-up and transfer are the agent's decisions**, via voice-channel tools
   (`end_call`, later `transfer_call`) that emit an event the worker executes.
   The prototype's goodbye regex stays only as a fallback.
9. **Limits.** Max call length, "Mai ești acolo?" after silence, hang-up after
   more silence (prototype values: 30 s / 15 s / 10 min, per-agent settings).
10. **End.** Duration and numbers are reported to ops for minute billing. The
    session is a normal `channel="voice"` session: Studio shows the transcript.

---

## 4. Turn handling

LiveKit's turn model has no Romanian, so the STT's own endpointing decides.

```python
turn_handling = {
    "turn_detection": "stt",
    "endpointing": {"min_delay": 0.05, "max_delay": 1.5},
    "interruption": {"mode": "vad", "min_duration": 0.6, "min_words": 2,
                     "resume_false_interruption": True, "false_interruption_timeout": 1.5},
    "preemptive_generation": {"enabled": False},  # the agent runs tools: no speculative turns
}
```

- RoSTT emits `START_OF_SPEECH` → `INTERIM_TRANSCRIPT` (STT partials) →
  `FINAL_TRANSCRIPT` → `END_OF_SPEECH`, **final before end-of-speech** (in
  `stt` mode LiveKit commits the turn on end-of-speech and ignores it if no
  transcript arrived yet). Empty `idle` finals are dropped.
- `min_words: 2` needs interim transcripts — RoSTT forwards STT partials.

---

## 5. Latency

Measured 2026-10-05 against prod speech services (from the dev laptop, tunnel):

| step | measured |
|---|---|
| STT final after the caller stops | **1.21 s** (640 ms pause + full re-encode + beam/LM) |
| TTS first bytes | **0.35 s**, of which **0.34 s is leading silence** |
| agent wake → first token | not yet measured (harness rebuilt every turn) |

Levers, in order of expected gain:
1. **Trim TTS silence** (lead and inter-sentence) — ~0.3 s per sentence.
2. **Harness warm path for voice** — cache MCP tool discovery and the KB list
   per session; voice turns exempt from the 30 s turn-gate backoff.
3. **Short first chunk** — send the first clause to TTS as soon as it ends.
4. **STT pause** — make the hard-coded 640 ms configurable in `surogate serve`
   and evaluate 500 ms.
5. **Model tier** — voice may use the faster `surogate` tier.

Every turn records `stt_final → first_token → first_audio` as a session event,
so these are tuned on numbers, not impressions.

---

## 6. Romanian text layer (ported from the prototype)

Applied in `tts_node`, never to the session text:
- **Pronunciations** (`SAY_AS`): per-agent table, editable in Studio, plus the
  brand rule (Surogate → "Surogheit").
- **Cleaning:** markdown, dashes → commas, `goldring.ro` → `goldring`, glued
  letters+digits (`A220` → `A 220`, `A220-300` → `A220`).
- **Sentence splitting** that does not split `E.ON`, `5.28`, `S.A.`, `nr.`,
  `Dl.`, `str.`.
- **Repeated preamble filter:** a second "o clipă, verific…" in one answer is dropped.
- **Echo filter:** a final that mostly repeats what we said in the last 20 s is
  dropped (speakerphones).
- **Phone audio:** the prototype's EQ (200 Hz high-pass, +4 dB presence at
  2.5 kHz, +10 dB with a soft limiter) applied to TTS PCM before LiveKit.

---

## 7. Capacity

- **STT:** 8 streams per pod; a call holds one for its whole length.
- **TTS:** `--max-num-seqs 4` per pod; used only while speaking.
- **Worker:** `load_fnc = active_calls / MAX_CALLS` per replica; LiveKit's
  default CPU-based load would not see STT saturation.

---

## 8. Ops: the Voice channel

- `phone_numbers` (number E.164, provider, status, monthly cost); admin page to
  add and disable numbers.
- Studio Voice card: pick a free number, greeting, voice (female/male), limits,
  pronunciations, "remember callers", transfer number (later).
- `voice` in `ChannelKind`, `CHANNEL_VOCAB`/`CHANNEL_LABELS`, managed-channel lists.
- Minute metering from call-ended reports; number fee monthly.

---

## 9. Prod infrastructure

Lives in the private ops repo (`k8s/voice-sip`): LiveKit and LiveKit SIP with host networking on
the node that holds the trunk's public IP, the firewall open to the carrier only, an inbound trunk
restricted to the carrier's address, outbound digest auth from a Secret. Ask the carrier for
RFC 4733 DTMF only (LiveKit SIP does not detect in-band tones).

Verified locally 2026-10-05: livekit-sip answers OPTIONS with 200 OK; an INVITE to `+40300000001`
and to `40300000001` matched the trunk and joined a room. The national form `03…` is unverified;
read the first real INVITE.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Caller-ID spoofing reaches another caller's memory | Decision 5: off by default; the agent never treats the number as identity. |
| One-way audio | `nat_1_to_1_ip` set explicitly; check the SIP start log. |
| STT saturation → dead air | Admission control + cached "busy" message. |
| Interrupted reply leaves false history | Pause + heard-text record (§3.6). |
| Cost abuse | Max call length, per-number concurrency, per-caller rate limit. |
| GDPR | Transcripts are sessions; if recording is added, the greeting says so. |

---

## 11. Phases

1. **`surogates voice` worker** — plugins, Romanian layer, session glue,
   tested locally (LiveKit in Docker, prod speech via tunnel, browser or
   softphone, scripted caller).
2. **surogates internals** — voice channel prompt, pause + heard text, call
   tools, harness warm path, turn metrics, per-caller memory toggle.
3. **ops** — numbers, Studio card, routing, billing.
4. **prod** — infra, first real call, remaining numbers, retire the Twilio bridge.
