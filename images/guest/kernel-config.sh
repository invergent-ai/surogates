#!/usr/bin/env bash
# Fails on a guest kernel whose configuration the guest cannot boot on. On virtio the VM boots it directly,
# with no initrd, so what it reads its root from, and the rest of what the guest needs before its init can
# load a module, is built in; and what vm/init loads, the entropy device's driver, the virtio-fs and 9P
# clients and overlay, is there, as a module or built in. On Hyper-V the VM boots it with the initrd
# (images/guest/initrd), so what unpacks the initrd and finds the disks once its drivers have loaded is built
# in; and those drivers are there. Each option missing is named. Run in the image's build on the pinned
# kernel's config-*, the modules package's, for each arch.
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
# An initrd; Hyper-V's own core, which its drivers build on; and SCSI's disks, which storvsc's are.
HYPERV_BUILT_IN=(BLK_DEV_INITRD HYPERV SCSI BLK_DEV_SD)
case "$ARCH" in
  # Hyper-V's clock, its reference TSC page; and the UART its console is on, ttyS0.
  amd64) HYPERV_BUILT_IN+=(HYPERV_TIMER SERIAL_8250_CONSOLE) ;;
  # The virt machine's PCIe host bridge: q35's is x86's own.
  arm64) BUILT_IN+=(PCI_HOST_GENERIC) ;;
  *) echo "usage: images/guest/kernel-config.sh amd64|arm64 <config>" >&2; exit 2 ;;
esac
LOADED=(VIRTIO_FS OVERLAY_FS NETFS_SUPPORT NET_9P NET_9P_FD 9P_FS HW_RANDOM_VIRTIO)
# Hyper-V's bus, its SCSI controller with the transport class it needs, its sockets with the family they
# are a transport of, and its balloon.
INITRD=(HYPERV_VMBUS SCSI_FC_ATTRS HYPERV_STORAGE VSOCKETS HYPERV_VSOCKETS HYPERV_BALLOON)

[ -r "$CONFIG" ] || { echo "kernel-config.sh: cannot read $CONFIG" >&2; exit 1; }
# Each option as the config sets it: y, m, or nothing for one it does not set.
set_to() { sed -n "s/^CONFIG_$1=\\(.*\\)\$/\\1/p" "$CONFIG"; }

# The options of a list, $2…, that the config does not set to one of $1's values.
lacking() {
  local values="$1" option
  shift
  for option; do
    case " $values " in *" $(set_to "$option") "*) ;; *) printf ' CONFIG_%s' "$option" ;; esac
  done
}
missing="$(lacking y "${BUILT_IN[@]}")"
unbooted="$(lacking y "${HYPERV_BUILT_IN[@]}")"
absent="$(lacking "y m" "${LOADED[@]}")"
undriven="$(lacking "y m" "${INITRD[@]}")"
[ -z "$missing" ] || echo "kernel-config.sh: $CONFIG does not build in${missing}, which the guest boots on without an initrd" >&2
[ -z "$unbooted" ] || echo "kernel-config.sh: $CONFIG does not build in${unbooted}, which the guest boots on with Hyper-V's initrd" >&2
[ -z "$absent" ] || echo "kernel-config.sh: $CONFIG has no${absent}, which vm/init loads" >&2
[ -z "$undriven" ] || echo "kernel-config.sh: $CONFIG has no${undriven}, which Hyper-V's initrd loads" >&2
[ -z "$missing$unbooted$absent$undriven" ]
