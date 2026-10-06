# Calling the agent locally

Everything runs on the laptop except the Romanian speech models, which are the prod ones through
an SSH tunnel. Nothing here changes prod.

1. **Local stack** (workspace root): `DEV_MODE=local ./dev.sh start`. On a fresh local DB, publish
   the built-in skills once, or every agent turn fails in the system-skills lookup:
   `cd surogate-ops && .venv/bin/surogate-ops seed-builtin-skills ~/.surogate/config.local.yaml --source ../surogates/skills`
2. **Speech tunnel** to the prod speech services (command in the private runbook):
   `the speech tunnel command from the private runbook (workspace AGENTS.md, "Voice channel locally")`
   Check: `curl -s http://127.0.0.1:18080/v1/audio/voices` lists `female` and `male`.
3. **LiveKit**: `scripts/voice-dev/livekit-up.sh` (server, SIP and Redis in Docker, a dev trunk for
   `+40300000001` and a dispatch rule to `surogate-voice`).
4. **A routing row** so the number reaches an agent (local ops DB only; `org_id` is the agent's
   project id, `id` has no default):
   ```sql
   -- docker exec -it sg-pg psql -U <user> -d surogate
   INSERT INTO channel_routing (id, channel_kind, channel_identifier, agent_id, org_id, config, active)
   VALUES (gen_random_uuid()::text, 'voice', '+40300000001', '<agent id>', '<project id>',
           '{"greeting": "Bună ziua! Sunt asistentul de test. Cu ce vă pot ajuta?", "voice": "female"}', true);
   ```
5. **The worker** (from `surogates/`):
   ```bash
   set -a; . ~/.surogate/local-only-runtime-token.env; set +a
   SUROGATES_CONFIG=~/.surogate/surogates.local.yaml SUROGATES_VOICE_LIVEKIT_API_KEY=devkey \
   SUROGATES_VOICE_LIVEKIT_API_SECRET=secret .venv/bin/surogates voice
   ```
6. **Call it.**
   - Scripted: `.venv/bin/python scripts/voice-dev/caller.py "Bună ziua. Ce poți face pentru mine?" "Mulțumesc, atât."`
     (the test line is a placeholder in this repo: set `VOICE_DEV_DID` for `livekit-up.sh` and
     `VOICE_QA_DID` for `caller.py`/`scenarios.py` to the number routed in your local stack)
     It joins as the SIP caller would, speaks each line with our TTS, and prints how long the agent
     took to start answering and what it said (through our STT).
   - By hand: call `sip:+40300000001@127.0.0.1:5060` from a softphone (Linphone, UDP, no account;
     G.711). livekit-sip announces its container IP for RTP; OrbStack routes container IPs from
     the Mac, so audio flows (on Docker Desktop it would not).
7. **Checks**, each with a fresh call:
   - Talk over a long answer: it stops within ~0.6 s; ask again — the agent knows where it was cut.
   - Say "da" / "aha" while it talks: it keeps talking.
   - Say "Mulțumesc, atât.": one farewell sentence, then the call ends.
   - Stay silent 30 s: "Mai sunteți acolo?", then goodbye after 15 s more.
   - Call a number with no routing row (`caller.py --called +40371000000 "Alo"`): "Acest număr nu
     este disponibil", the call ends.
8. **Where the time goes**: per turn, from the session's events (`surogates` DB):
   `user.message → llm.request` is the harness wake, `→ first llm.delta` is the model's first token.
9. **Stop**: `docker rm -f lk-sip livekit lk-redis && docker network rm lkdev`, stop the worker,
   `DEV_MODE=local ./dev.sh stop`, and close the tunnel (`pkill -f 18001:`).
