// The sandbox for one folder (spec, Section 4): nothing readable but the system,
// the app, the folder, its temp folder and the user's toolchains; nothing
// writable but the folder and the temp folder; the network only to the package hosts.

import { existsSync } from "node:fs";
import { join } from "node:path";

import { inside } from "../files/paths.js";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

const SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"];
// srt reads these in a policy path as a glob: allowRead widens, allowWrite drops the path.
export const GLOB = /[*?[\]]/;
// How deep srt's scan for nested protected names looks, from the folder: its maximum (its default is 3).
export const SCAN_DEPTH = 10;
const TOOLCHAINS = [".nvm", ".pyenv", ".rustup", ".cargo/bin", ".local/bin", ".local/lib", "go", ".bun", ".deno", ".sdkman"];

// srt binds its own temp folder read-write into every sandbox, a channel shared with all the others.
const SRT_TMP = ["/tmp/claude", "/private/tmp/claude"];

// The package hosts commands may reach without asking: the cloud's list
// (surogates/tools/workspace_io/local.py) without its coding-agent endpoints.
// srt's proxy refuses every other host with a 403 until approvals exist. srt reads
// the list globally, so it is the whole host's, the file helper's included (it
// makes no network calls).
export const PACKAGE_HOSTS = [
  "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "files.pythonhosted.org",
  "npmjs.org", "*.npmjs.org", "registry.npmjs.org",
];

// srt binds its own /tmp/claude into every sandbox whenever it exists, whatever
// the policy says, and other srt users (Claude Code among them) keep files there.
// A later mount wins, so an empty tmpfs goes over it, just after srt's last
// mount (0.0.77). If srt's line ever lacks that anchor, no sandbox starts
// rather than one that shows /tmp/claude.
const MOUNTS_END = " --dev /dev --unshare-pid ";
export function hideSrtTmp(line: string): string {
  const at = unquotedIndex(line, MOUNTS_END);
  if (at < 0) throw new Error("srt's sandbox command has changed: cannot hide /tmp/claude");
  return `${line.slice(0, at)} --tmpfs /tmp/claude${line.slice(at)}`;
}

// One word on a bash line: in '...', an embedded quote written as '\''.
export const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

// Where *text* first appears among the line's words, never inside a quoted word:
// srt's scan puts paths the agent named in the folder on the line, and one can hold
// the anchor. srt quotes a word in '...' and writes an embedded quote as "'".
function unquotedIndex(line: string, text: string): number {
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === "'" || ch === '"') {
      const end = line.indexOf(ch, i + 1);
      if (end < 0) return -1;
      i = end;
    } else if (line.startsWith(text, i)) {
      return i;
    }
  }
  return -1;
}

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
  socatPath?: string;
  rgPath?: string;
}

export function sandboxPolicy({ folder, tmp, home, appDirs, bwrapPath, socatPath, rgPath }: PolicyInput): SandboxRuntimeConfig {
  return {
    ...(bwrapPath ? { bwrapPath } : {}),
    ...(socatPath ? { socatPath } : {}),
    // srt's scan for nested protected names (rg, from the folder) must not read the
    // folder's .ignore, .rgignore or .gitignore: the agent writes those, and one
    // naming a nested repo would leave its .git/config writable.
    ripgrep: { command: rgPath ?? "rg", args: ["--no-ignore"] },
    mandatoryDenySearchDepth: SCAN_DEPTH,
    network: { allowedDomains: PACKAGE_HOSTS, deniedDomains: [] },
    filesystem: {
      denyRead: ["/"],
      allowRead: [
        ...SYSTEM, ...appDirs, folder, tmp,
        ...TOOLCHAINS.map((name) => join(home, name)).filter((path) => existsSync(path) && !GLOB.test(path)),
      ],
      allowWrite: [folder, tmp],
      // hideSrtTmp hides /tmp/claude under an empty tmpfs; this keeps it read-only
      // even where that tmpfs did not apply.
      denyWrite: SRT_TMP,
    },
  };
}
