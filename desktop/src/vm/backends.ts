// The VM layer's backend for each OS, in one table (spec, Section 11): what boots the VM, what of
// this computer the VM needs, where its runtime files go, and how long a socket's path may be
// there. The VM manager, the app's start and the VM's files take each from here; an OS whose
// backend is not built yet has none, and its sandbox is not available.

import { createHash } from "node:crypto";
import { join } from "node:path";

import type { HostUser } from "../guest/protocol.js";
import { bootLinux, missingTools } from "./linux.js";
import type { BootVm } from "./manager.js";

export interface Backend {
  boot: BootVm;
  /**
   * What this computer lacks of what its VM runs on, as Section 11's table names it: with
   * *unpacking*, while the image has something left to unpack; its tools looked for on *path*,
   * the app's PATH, in no folder of *held*, the folders chats are bound to.
   */
  requirements(ask: { unpacking: boolean; held: Array<{ dev: number; ino: number }>; path?: string }): Promise<string[]>;
  /** The user's own 0700 folder for the VM's sockets and pidfiles, a data root's own. */
  runtimeFolder(user: HostUser, env: NodeJS.ProcessEnv, dataDir: string): string;
  /** The longest Unix socket path this OS takes, in bytes, without the NUL. */
  socketMax: number;
}

const LINUX: Backend = {
  boot: bootLinux,
  requirements: ({ unpacking, held, path }) => missingTools({}, unpacking, held, path),
  // The user's own, 0700, made by logind; short enough for a vhost-user socket's 108 bytes. A folder
  // for each data folder, so two apps never share one (a development build beside the installed
  // app, a test), and a boot's sweep reaches no other app's guest.
  runtimeFolder: (user, env, dataDir) => join(env.XDG_RUNTIME_DIR || `/run/user/${user.uid}`, "surogate", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`),
  socketMax: 107,
};

/** The backend for *platform*: Linux's, or null on an OS whose backend is not built yet. */
export function backendFor(platform: NodeJS.Platform): Backend | null {
  return platform === "linux" ? LINUX : null;
}
