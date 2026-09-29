#!/usr/bin/env bash
set -euo pipefail
APP=~/.local/share/surogate/versions/spike/surogate
R=~/surogate-spike-results
for victim in host main; do
  rm -f "$R/q8-journal.jsonl" "$R/q8-host.pid" "$R/q8-main.pid" "$R/q8-start-info.json"
  xvfb-run -a "$APP" q8 & DRIVER=$!
  for _ in $(seq 1 30); do [ -s "$R/q8-host.pid" ] && [ -s "$R/q8-start-info.json" ] && break; sleep 1; done
  sleep 3
  # Evidence that the trees are running before the kill (plain and scope marks).
  echo "{\"victim\":\"$victim\",\"plain\":$(grep -l "SPIKE_MARK=q8-plain" /proc/[0-9]*/environ 2>/dev/null | wc -l),\"scope\":$(grep -l "SPIKE_MARK=q8-scope" /proc/[0-9]*/environ 2>/dev/null | wc -l),\"scopeUnits\":$(systemctl --user list-units --no-legend 'surogate-spike-*' 2>/dev/null | wc -l)}" > "$R/q8-prekill-$victim.json"
  kill -9 "$(cat "$R/q8-$victim.pid")"
  sleep 3
  xvfb-run -a "$APP" q8-recover "$victim"
  kill "$DRIVER" 2>/dev/null || true
  pkill -9 -x surogate 2>/dev/null || true   # exact name: -f would match this script's own ssh shell
  # Survivors are recorded by q8-recover; clear them so the next iteration starts clean.
  for p in $(grep -l "SPIKE_MARK=q8" /proc/[0-9]*/environ 2>/dev/null | cut -d/ -f3); do kill -9 "$p" 2>/dev/null || true; done
  sleep 2
done
jq . "$R"/q8-host-*.json "$R"/q8-main-*.json
