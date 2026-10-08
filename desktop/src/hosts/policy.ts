// The file helper's sandbox (spec, Section 11, "The file host, still in srt"): nothing
// readable but the system, the app, the folder and the helper's working folder; nothing
// writable but the folder and that working folder; no network.

import { findOnPath } from "../files/operations.js";
import { inside } from "../files/paths.js";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

const SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"];
// Section 9's words for a computer that lacks what the sandbox runs on, and what it lacks.
export const toolsMissing = (lacking: string[]) => `Surogate's sandbox tools are missing. Run the install script again. It lacks ${lacking.join(", ")}`;

/**
 * What the file helper's sandbox lacks of this computer, each named as the install script
 * installs it: bubblewrap, at *bwrap* (the installed version's own copy) or else on *path*;
 * socat, which srt runs; and ripgrep, which the helper searches with.
 */
export function fileToolsMissing(bwrap: string | undefined, path: string): string[] {
  return [["bubblewrap", bwrap ?? "bwrap"], ["socat", "socat"], ["ripgrep", "rg"]]
    .filter(([, program]) => findOnPath(program!, path, "/") === null).map(([name]) => name!);
}

// srt reads these in a policy path as a glob: allowRead widens, allowWrite drops the path.
export const GLOB = /[*?[\]]/;

// srt binds its own temp folder read-write into every sandbox, a channel shared with all the others.
const SRT_TMP = ["/tmp/claude", "/private/tmp/claude"];

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
  tmp: string; // the helper's working folder, which srt's placeholders go in
  appDirs: string[];
  bwrapPath?: string;
  socatPath?: string;
}

export function sandboxPolicy({ folder, tmp, appDirs, bwrapPath, socatPath }: PolicyInput): SandboxRuntimeConfig {
  return {
    ...(bwrapPath ? { bwrapPath } : {}),
    ...(socatPath ? { socatPath } : {}),
    // No host: the helper connects to nothing. An empty list, not none, keeps srt's own network namespace.
    network: { allowedDomains: [], deniedDomains: [] },
    filesystem: {
      denyRead: ["/"],
      allowRead: [...SYSTEM, ...appDirs, folder, tmp],
      allowWrite: [folder, tmp],
      // hideSrtTmp hides /tmp/claude under an empty tmpfs; this keeps it read-only
      // even where that tmpfs did not apply.
      denyWrite: SRT_TMP,
    },
  };
}
