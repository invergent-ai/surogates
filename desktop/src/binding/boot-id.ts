// This boot's identity, as each OS names it. A folder's st_dev belongs to its mount and can change at a reboot
// (btrfs subvolumes, ZFS, NFS and SMB, FUSE, several NVMe drives), so a binding records the boot it was made in.

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { cleanly } from "../clean-child.js";
import { forThisOs } from "../platform.js";

const read = forThisOs<() => string>({
  // The kernel's boot id.
  linux: () => readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim(),
  // The kernel's boot session UUID, which sysctl(8) reads for any user; started cleanly, as every program the app runs.
  darwin() {
    const [file, args] = cleanly("/usr/sbin/sysctl", ["-n", "kern.bootsessionuuid"]);
    const read = spawnSync(file, args, { encoding: "utf8", env: {}, stdio: ["ignore", "pipe", "ignore"], timeout: 5_000 });
    return read.status === 0 ? read.stdout.trim() : "";
  },
// Another OS has none until its own is read here.
}) ?? (() => "");

/** This boot's identity; "" where it cannot be read, which counts as this boot. */
export function bootId(): string {
  try {
    return read();
  } catch {
    return "";
  }
}
