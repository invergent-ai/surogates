// The executor of the file and process kinds, and the VM, as the app makes them, in a module of
// their own: the app's main makes its own through it, and so do the acceptance VMs' probes, second
// mains in an installed version (test/acceptance/probe.mjs, guest-probe.mjs). What a file host and
// the VM are given here is then what the probes run on a clean Ubuntu, where the wrong bwrap is refused.

import { dirname, join } from "node:path";

import { app } from "electron";

import { VmClient, type VmClientOptions } from "../vm/client.js";
import { VmExecutor, type VmExecutorOptions } from "../vm/executor.js";

// The installed app gives srt its version's own copy of the system's bwrap, which the install script
// makes beside it: the copy takes the app's AppArmor profile, never the one Ubuntu attaches to
// /usr/bin/bwrap (spec, Section 4). A development build finds bwrap on its PATH.
export const BWRAP = app.isPackaged ? join(dirname(process.execPath), "bin", "bwrap") : undefined;

/** The file kinds in each root's file host, on this version's bwrap when installed, and the process kinds in the VM. */
export function appTools(options: Omit<VmExecutorOptions, "bwrapPath">): VmExecutor {
  return new VmExecutor({ ...options, bwrapPath: BWRAP });
}

/** The VM, whose folders the guest reads alone are shared through this version's bwrap when installed. */
export function appVm(options: VmClientOptions): VmClient {
  return new VmClient({ ...options, vm: { ...options.vm, ...(BWRAP ? { bwrap: BWRAP } : {}) } });
}
