#!/usr/bin/env bash
# Fails on a guest kernel whose configuration the guest cannot boot on: the VM boots it directly,
# with no initrd, so what it reads its root from, and the rest of what the guest needs before its
# init can load a module, is built in; and what vm/init loads, the entropy device's driver,
# virtiofs and overlay, is there, as a module or built in. Each option missing is named. Run in the image's build on the pinned kernel's
# config-*, the modules package's, for each arch.
#
# Usage:
#   images/guest/kernel-config.sh amd64|arm64 <config>
set -euo pipefail

ARCH="${1:-}"
CONFIG="${2:-}"
# virtio-blk and ext4, the disks; the virtio console, hvc0 and the agent's ports; virtio on PCI,
# where QEMU puts every device; BPF-LSM and the kernel's BTF, which the rule needs; FUSE, which
# virtiofs is; and the hardware random-number core, which the entropy device's driver plugs into,
# since the image keeps no module but those vm/init loads.
BUILT_IN=(VIRTIO_BLK EXT4_FS VIRTIO_CONSOLE VIRTIO_PCI BPF_LSM DEBUG_INFO_BTF FUSE_FS HW_RANDOM)
case "$ARCH" in
  amd64) ;;
  # The virt machine's PCIe host bridge: q35's is x86's own.
  arm64) BUILT_IN+=(PCI_HOST_GENERIC) ;;
  *) echo "usage: images/guest/kernel-config.sh amd64|arm64 <config>" >&2; exit 2 ;;
esac
LOADED=(VIRTIO_FS OVERLAY_FS HW_RANDOM_VIRTIO)

[ -r "$CONFIG" ] || { echo "kernel-config.sh: cannot read $CONFIG" >&2; exit 1; }
# Each option as the config sets it: y, m, or nothing for one it does not set.
set_to() { sed -n "s/^CONFIG_$1=\\(.*\\)\$/\\1/p" "$CONFIG"; }

missing=()
for option in "${BUILT_IN[@]}"; do
  [ "$(set_to "$option")" = y ] || missing+=("CONFIG_$option")
done
absent=()
for option in "${LOADED[@]}"; do
  case "$(set_to "$option")" in y | m) ;; *) absent+=("CONFIG_$option") ;; esac
done
[ "${#missing[@]}" -eq 0 ] || echo "kernel-config.sh: $CONFIG does not build in ${missing[*]}, which the guest boots on without an initrd" >&2
[ "${#absent[@]}" -eq 0 ] || echo "kernel-config.sh: $CONFIG has no ${absent[*]}, which vm/init loads" >&2
[ "${#missing[@]}" -eq 0 ] && [ "${#absent[@]}" -eq 0 ]
