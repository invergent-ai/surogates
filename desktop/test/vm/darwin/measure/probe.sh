# Measurements only, never the app's (prepare.sh puts this in the measurements' agent disk's init, before
# the agent starts): a probe a measurement leaves in a shared folder, .measure-probe, run as the guest's
# root, its output left beside it as .measure-probe.out: a root's folder, or a folder's place's history.
( while :; do
    for probe in /run/surogate/shares/*/.measure-probe /run/surogate/places/*/history/.measure-probe; do
      [ -f "$probe" ] || continue
      sh "$probe" > "$probe.partial" 2>&1 || true
      mv "$probe.partial" "$probe.out"
      rm -f "$probe"
    done
    sleep 0.2
  done ) &
# Where a boot's time went (boot.ts), on the console: the guest's uptime as the agent starts, and as the rule's
# outcome lands, then the kernel's log so far, each line with its time since the kernel's start.
echo "surogate-measure: uptime $(cut -d' ' -f1 /proc/uptime) s, the agent's start" >&2
( while [ ! -f /run/surogate/rule ]; do sleep 0.01; done
  echo "surogate-measure: uptime $(cut -d' ' -f1 /proc/uptime) s, the rule's outcome: $(cat /run/surogate/rule)" >&2 ) &
dmesg --color=never >&2 || true
