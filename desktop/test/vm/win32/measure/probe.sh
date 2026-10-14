# Measurements only, never the app's (the vm-guest job of .github/workflows/desktop-windows.yml puts this in the
# measurements' agent disk's init, before the agent starts): a probe a measurement leaves in a shared folder,
# .measure-probe, run as the guest's root, its output left beside it as .measure-probe.out.
( while :; do
    for probe in /run/surogate/shares/*/.measure-probe /run/surogate/shares/*/*/.measure-probe; do
      [ -f "$probe" ] || continue
      sh "$probe" > "$probe.partial" 2>&1 || true
      mv "$probe.partial" "$probe.out"
      rm -f "$probe"
    done
    sleep 0.2
  done ) &
# Where a boot's time went (boot.ts), on the console: the guest's uptime as the agent starts, and as the rule's
# outcome lands; then, a second after that, by when the agent has said hello, the kernel's log so far, each line with
# its time since the kernel's start. Not all of it, as macOS's probe gives it: Hyper-V's COM1 carries under 2 KB a
# second where its VM is nested, so each line that came 50 ms or more after the one before it, with that one and the
# wait between them; the lines of the boot's milestones; and a line that says it has ended.
echo "surogate-measure: uptime $(cut -d' ' -f1 /proc/uptime) s, the agent's start" >&2
( while [ ! -f /run/surogate/rule ]; do sleep 0.01; done
  echo "surogate-measure: uptime $(cut -d' ' -f1 /proc/uptime) s, the rule's outcome: $(cat /run/surogate/rule)" >&2
  sleep 1
  dmesg --color=never | awk '
    {
      t = substr($0, 2, index($0, "]") - 2) + 0
      waited = NR > 1 && t - last >= 0.05
      if (waited) {
        if (!shown) print previous
        printf "surogate-measure: the kernel waited %d ms\n", (t - last) * 1000
      }
      shown = waited || /Run \/init as init process|Freeing unused kernel image|Attached SCSI disk|EXT4-fs \(|crng init done/
      if (shown) print
      last = t
      previous = $0
    }
    END { if (!shown) print previous }' >&2 || true
  echo "surogate-measure: the kernel's log ends" >&2 ) &
