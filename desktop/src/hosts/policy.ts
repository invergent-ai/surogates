// The sandbox for one folder (spec, Section 4): nothing readable but the system,
// the app, the folder, its temp folder and the user's toolchains; nothing
// writable but the folder and the temp folder; no network.

import { existsSync } from "node:fs";
import { join } from "node:path";

import { inside } from "../files/paths.js";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

const SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"];
// srt reads these in a policy path as a glob: allowRead widens, allowWrite drops the path.
export const GLOB = /[*?[\]]/;
const TOOLCHAINS = [".nvm", ".pyenv", ".rustup", ".cargo/bin", ".local/bin", ".local/lib", "go", ".bun", ".deno", ".sdkman"];

// srt binds its own temp folder read-write into every sandbox, a channel shared with all the others.
const SRT_TMP = ["/tmp/claude", "/private/tmp/claude"];

// A folder the sandbox must never be given: the kernel's and the devices' folders; /run, which holds the
// user's sockets (the session bus, the agents), but not /run/media, where drives are mounted; and srt's
// temp folder, which the policy makes read-only.
export function isReserved(path: string): boolean {
  return (
    ["/proc", "/sys", "/dev"].some((dir) => inside(path, dir)) ||
    (inside(path, "/run") && !inside(path, "/run/media")) ||
    SRT_TMP.some((dir) => inside(path, dir))
  );
}

export interface PolicyInput {
  folder: string;
  tmp: string;
  home: string;
  appDirs: string[];
  bwrapPath?: string;
}

export function sandboxPolicy({ folder, tmp, home, appDirs, bwrapPath }: PolicyInput): SandboxRuntimeConfig {
  return {
    ...(bwrapPath ? { bwrapPath } : {}),
    network: { allowedDomains: [], deniedDomains: [] },
    filesystem: {
      denyRead: ["/"],
      allowRead: [
        ...SYSTEM, ...appDirs, folder, tmp,
        ...TOOLCHAINS.map((name) => join(home, name)).filter((path) => existsSync(path) && !GLOB.test(path)),
      ],
      allowWrite: [folder, tmp],
      // It stays readable: an open item for commands.
      denyWrite: SRT_TMP,
    },
  };
}
