// The VM layer's backend for each OS, in one table (spec, Section 11): what boots the VM, what of
// this computer the VM needs, where its runtime files go, where the guest's ports and the browser's
// door are, and how long a socket's path may be there. The VM manager, the app's start and the VM's files take each from here; an OS whose
// backend is not built yet has none, and its sandbox is not available. Linux's is QEMU (linux.ts),
// macOS's the Virtualization framework through surogate-vm (darwin.ts).

import { createHash } from "node:crypto";

import type { HostUser } from "../guest/protocol.js";
import { join } from "../host-path.js";
import { forThisOs, notBuilt } from "../platform.js";
import { bootDarwin, requirementsDarwin, runtimeFolder, SOCKET_MAX } from "./darwin.js";
import { bootLinux, missingTools } from "./linux.js";
import { addressesWin32, bootWin32, requirementsWin32, runtimeFolderWin32 } from "./win32.js";
import type { BootVm } from "./manager.js";
import { socketsIn } from "./sockets.js";

/** Where the VM manager reaches the guest's control, net and inbound ports, and the browser's door: Unix socket paths, or pipe names. */
export interface Addresses {
  control: string;
  net: string;
  inbound: string;
  door: string;
}

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
  /** Where the VM manager reaches the guest's three ports, and where the browser's door is, for the runtime folder *run*. */
  addresses(run: string, user: HostUser): Addresses;
  /** The longest Unix socket path this OS takes, in bytes, without the NUL, or null where the ports are not Unix sockets. */
  socketMax: number | null;
}

const LINUX: Backend = {
  boot: bootLinux,
  requirements: ({ unpacking, held, path }) => missingTools({}, unpacking, held, path),
  // The user's own, 0700, made by logind; short enough for a vhost-user socket's 108 bytes. A folder
  // for each data folder, so two apps never share one (a development build beside the installed
  // app, a test), and a boot's sweep reaches no other app's guest.
  runtimeFolder: (user, env, dataDir) => join(env.XDG_RUNTIME_DIR || `/run/user/${user.uid}`, "surogate", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`),
  // QEMU's chardevs, made where the boot names them (qemu.ts), and the manager's door.
  addresses: (run) => socketsIn(run),
  socketMax: 107,
};

const DARWIN: Backend = {
  boot: bootDarwin,
  requirements: () => requirementsDarwin(),
  // The user's own, 0700, under /var/folders: short enough for Darwin's 104-byte socket paths. A folder for each data folder.
  runtimeFolder: (_user, env, dataDir) => (env.TMPDIR ? runtimeFolder(dataDir, env.TMPDIR) : runtimeFolder(dataDir)),
  // The sockets surogate-vm makes where it is told (darwin.ts), and the manager's door.
  addresses: (run) => socketsIn(run),
  socketMax: SOCKET_MAX,
};

const WIN32: Backend = {
  boot: bootWin32,
  // No tool on the PATH: the image is unpacked with Node's own zstd, and the file host is the file tools' (milestone D).
  requirements: () => requirementsWin32(),
  // This user's own, under %LOCALAPPDATA% (not Roaming, which would copy the sessions disk at sign-out), one per data folder.
  runtimeFolder: (_user, env, dataDir) => runtimeFolderWin32(env, dataDir),
  // The named pipes surogate-vm.exe makes, only this user's to open, and the manager's door.
  addresses: (run) => addressesWin32(run),
  // The ports are named pipes, not Unix sockets: no path-length limit.
  socketMax: null,
};

/** The backend for *platform*: Linux's, macOS's or Windows's, or null on an OS whose backend is not built yet. */
export function backendFor(platform: NodeJS.Platform): Backend | null {
  return forThisOs({ linux: LINUX, darwin: DARWIN, win32: WIN32 }, platform);
}

/** This computer's backend, or its refusal if the app's sandbox is not built for this OS: a value no caller may default. */
export function backendHere(): Backend {
  return backendFor(process.platform) ?? notBuilt("This computer's sandbox");
}
