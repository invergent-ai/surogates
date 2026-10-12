// This boot's identity, as each OS names it. A folder's st_dev belongs to its mount and can change at a reboot
// (btrfs subvolumes, ZFS, NFS and SMB, FUSE, several NVMe drives), so a binding records the boot it was made in.

import { readFileSync } from "node:fs";

// Linux's is the kernel's boot id. Another OS has none until its own is read here.
const read = process.platform === "linux" ? () => readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim() : () => "";

/** This boot's identity; "" where it cannot be read, which counts as this boot. */
export function bootId(): string {
  try {
    return read();
  } catch {
    return "";
  }
}
