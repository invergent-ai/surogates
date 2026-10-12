// The file helper's sandbox (spec, Section 11, "The file host, still in srt"): nothing
// readable but the system, the app, the folder and the helper's working folder; nothing
// writable but the folder and that working folder; no network. A landing's helper also reads the
// thread's copy it lands from, and writes the folder its replaced files are kept in (Section 13).
// What of it is each OS's own is in srt-os.ts.

import { statSync } from "node:fs";
import { dirname } from "node:path";

import { inside, realpath } from "../files/paths.js";
import { srtOs } from "./srt-os.js";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

export { hideSrtTmp } from "./srt-linux.js";

// Section 9's words for a computer that lacks what the sandbox runs on, and what it lacks.
export const toolsMissing = (lacking: string[]) => srtOs.toolsMissing(lacking);

/**
 * What the file helper's sandbox lacks of this computer, each named as a person installs it. On
 * Linux: bubblewrap, at *bwrap* (the installed version's own copy) or else on *path*; socat, which
 * srt runs; and ripgrep, which the helper searches with. On macOS, ripgrep alone.
 */
export function fileToolsMissing(bwrap: string | undefined, path: string): string[] {
  return srtOs.fileToolsMissing(bwrap, path);
}

/**
 * The PATH a file host looks on, of *path*, the app's own: each absolute entry, kept as the
 * path it leads to now, so a link a command swaps in later moves none. An entry is dropped
 * when it is one of *held*, the folders a command may write, or lies in one, by what each is
 * (dev:ino), however it is spelled: a link, a bind mount, a .. through a folder in it. With
 * none left, the system's own. A file host holds its folder and its working folder; the app
 * looks for the file helper's tools by the same rule, holding every folder its chats are bound to.
 */
export function pathOutside(path: string | undefined, held: Array<{ dev: number; ino: number }>): string {
  const ids = held.map(({ dev, ino }) => `${dev}:${ino}`);
  const within = (dir: string): boolean => {
    for (let at = dir; ; at = dirname(at)) {
      let stats;
      try {
        stats = statSync(at, { throwIfNoEntry: false });
      } catch {
        return true; // a folder that cannot be judged (ENOTDIR, EACCES, ELOOP): its entry is dropped
      }
      if (stats && ids.includes(`${stats.dev}:${stats.ino}`)) return true;
      if (at === dirname(at)) return false;
    }
  };
  return (path ?? "").split(":")
    .filter((entry) => entry.startsWith("/"))
    .map((entry) => realpath(entry).path)
    .filter((entry) => !within(entry))
    .join(":") || "/usr/bin:/bin";
}

// srt reads these in a policy path as a glob: allowRead widens, allowWrite drops the path.
export const GLOB = /[*?[\]]/;

// The most bytes of a folder's path this OS's sandbox takes, or null for no bound beside the system's own.
export const FOLDER_BYTES = srtOs.folderBytes;

// srt binds its own temp folder read-write into every sandbox, a channel shared with all the others.
const SRT_TMP = ["/tmp/claude", "/private/tmp/claude"];

// One word on a bash line: in '...', an embedded quote written as '\''.
export const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

// A folder the sandbox must never be given: this OS's own (srt-os.ts), and srt's temp folder, which the policy
// makes read-only.
export function isReserved(path: string): boolean {
  return srtOs.reserved(path) || SRT_TMP.some((dir) => inside(path, dir));
}

export interface PolicyInput {
  folder: string;
  tmp: string; // the helper's working folder, which srt's placeholders go in
  appDirs: string[];
  bwrapPath?: string;
  socatPath?: string;
  reads?: string[]; // what else it may read, and not write: a landing's copy
  writes?: string[]; // what else it may write: a landing's kept folder
}

export function sandboxPolicy({ folder, tmp, appDirs, bwrapPath, socatPath, reads = [], writes = [] }: PolicyInput): SandboxRuntimeConfig {
  return {
    ...(bwrapPath ? { bwrapPath } : {}),
    ...(socatPath ? { socatPath } : {}),
    // No host: the helper connects to nothing. An empty list, not none, keeps srt's own network namespace.
    network: { allowedDomains: [], deniedDomains: [] },
    filesystem: {
      denyRead: ["/"],
      allowRead: [...srtOs.system, ...appDirs, folder, tmp, ...reads, ...writes],
      allowWrite: [folder, tmp, ...writes],
      // On Linux hideSrtTmp hides /tmp/claude under an empty tmpfs; this keeps it read-only
      // even where that tmpfs did not apply. On macOS the denied read of / hides it.
      denyWrite: SRT_TMP,
    },
  };
}
