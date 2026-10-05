#!/usr/bin/env bash
# LiveKit server + SIP + Redis in Docker, with the dev trunk and dispatch rule.
# Dev keys (devkey/secret) only; nothing here talks to prod.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DID="${VOICE_DEV_DID:-+40300000001}"
docker network inspect lkdev >/dev/null 2>&1 || docker network create lkdev >/dev/null
docker run -d --rm --name lk-redis --network lkdev redis:7-alpine >/dev/null
docker run -d --rm --name livekit --network lkdev -p 127.0.0.1:7880:7880 \
  -e LIVEKIT_CONFIG="redis: {address: lk-redis:6379}" livekit/livekit-server:latest --dev --bind 0.0.0.0 >/dev/null
docker run -d --rm --name lk-sip --network lkdev -p 127.0.0.1:5060:5060/udp -p 127.0.0.1:10000-10100:10000-10100/udp \
  -v "$HERE/sip.yaml:/sip/config.yaml:ro" livekit/sip:latest >/dev/null
TMP="$(mktemp -d)"
lk() { docker run --rm --network lkdev -v "$TMP:/w" -w /w livekit/livekit-cli --url http://livekit:7880 --api-key devkey --api-secret secret "$@"; }
sleep 3
# JSON request files: the shape verified against livekit-sip on 2026-10-05
printf '{"trunk": {"name": "dev-inbound", "numbers": ["%s"]}}\n' "$DID" > "$TMP/inbound.json"
TRUNK=$(lk sip inbound create inbound.json | awk '/SIPTrunkID/{print $2}')
printf '{"dispatch_rule": {"name": "dev-voice", "trunk_ids": ["%s"], "rule": {"dispatchRuleIndividual": {"roomPrefix": "call-"}}, "room_config": {"agents": [{"agent_name": "surogate-voice"}]}}}\n' "$TRUNK" > "$TMP/dispatch.json"
lk sip dispatch create dispatch.json >/dev/null
echo "LiveKit ws://127.0.0.1:7880 (devkey/secret), SIP udp 127.0.0.1:5060, trunk $TRUNK for $DID"
echo "stop: docker rm -f lk-sip livekit lk-redis && docker network rm lkdev"
