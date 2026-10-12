# Measurements only, never the app's (prepare.sh puts this in the measurements' agent disk's init, before
# the agent starts): a probe a measurement leaves in a shared folder, .measure-probe, run as the guest's
# root, its output left beside it as .measure-probe.out.
( while :; do
    for probe in /run/surogate/shares/*/.measure-probe /run/surogate/shares/*/*/.measure-probe; do
      [ -f "$probe" ] || continue
      sh "$probe" > "$probe.partial" 2>&1
      mv "$probe.partial" "$probe.out"
      rm -f "$probe"
    done
    sleep 0.2
  done ) &
