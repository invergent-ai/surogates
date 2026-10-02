// The sandbox for one folder (spec, Section 4): nothing readable but the system,
// the app, the folder, its temp folder and the user's toolchains; nothing
// writable but the folder and the temp folder; no network.

import { existsSync } from "node:fs";
import { join } from "node:path";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

const SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"];
const TOOLCHAINS = [".nvm", ".pyenv", ".rustup", ".cargo/bin", ".local/bin", ".local/lib", "go", ".bun", ".deno", ".sdkman"];

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
        ...TOOLCHAINS.map((name) => join(home, name)).filter((path) => existsSync(path)),
      ],
      allowWrite: [folder, tmp],
      denyWrite: [],
    },
  };
}
