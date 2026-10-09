// The executor of the file and process kinds as the app makes it, in a module of its own: the
// app's main makes its own through it, and so does the acceptance VMs' probe, a second main in an
// installed version (test/acceptance/probe.mjs). What a file host is given here is then what the
// probe runs on a clean Ubuntu, where the wrong bwrap is refused.

import { dirname, join } from "node:path";

import { app } from "electron";

import { VmExecutor, type VmExecutorOptions } from "../vm/executor.js";

// The installed app gives srt its version's own copy of the system's bwrap, which the install script
// makes beside it: the copy takes the app's AppArmor profile, never the one Ubuntu attaches to
// /usr/bin/bwrap (spec, Section 4). A development build finds bwrap on its PATH.
const BWRAP = app.isPackaged ? join(dirname(process.execPath), "bin", "bwrap") : undefined;

/** The file kinds in each root's file host, on this version's bwrap when installed, and the process kinds in the VM. */
export function appTools(options: Omit<VmExecutorOptions, "bwrapPath">): VmExecutor {
  return new VmExecutor({ ...options, bwrapPath: BWRAP });
}
