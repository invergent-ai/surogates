// Which folders a chat may work on (spec, Section 4): checked when the user
// confirms one, and again by its tool host before anything runs there.

import { readFileSync, realpathSync, type Stats, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { inside, realpath } from "../files/paths.js";
import { GLOB, isReserved } from "../hosts/policy.js";

const CREDENTIALS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];

// This boot, read once; "" when it cannot be read. A folder's st_dev belongs to its mount and
// can change at a reboot (btrfs subvolumes, ZFS, NFS and SMB, FUSE, several NVMe drives).
export const BOOT_ID = (() => {
  try {
    return readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
  } catch {
    return "";
  }
})();

// What a chat's folder must keep clear of: the home folder, the app's own data
// and files (which run outside the sandbox), and the credential folders.
export interface FolderGuards {
  home: string;
  dataDir: string;
  appDirs: string[];
}

export type FolderCheck =
  | { ok: true; path: string; dev: number; ino: number }
  // missing: the folder is not there, or is not a folder; otherwise it may never be a chat's.
  | { ok: false; missing: boolean; message: string };

// A path as spelled, and as the file system resolves it. Where it does not exist yet, or cannot be
// read, the part that exists is still resolved: a guard that is not there yet is still where its links lead.
export function spellings(path: string): string[] {
  const plain = resolve(path);
  const { path: real } = realpath(plain);
  return real === plain ? [plain] : [plain, real];
}

// The folder as it resolves, and its identity; or why no chat may work on it.
export function checkFolder(folder: string, guards: FolderGuards): FolderCheck {
  let path: string;
  let stats: Stats;
  // A name that is not valid UTF-8 reads back with U+FFFD, which opens nothing: such a folder is not there.
  try {
    path = realpathSync(folder);
    stats = statSync(path);
  } catch {
    return { ok: false, missing: true, message: `the folder ${folder} is not there` };
  }
  if (!stats.isDirectory()) return { ok: false, missing: true, message: `the folder ${folder} is not a folder` };
  const refused = (message: string): FolderCheck => ({ ok: false, missing: false, message });
  if (GLOB.test(path)) return refused(`this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${path}`);
  if (isReserved(path)) return refused(`the folder ${path} is inside one of this computer's system folders`);
  // A sandbox whose writable folder held the home folder, the app's own data or
  // files or a credential folder would hand all of it to the agent. Each is
  // compared as spelled and as resolved: a link would otherwise walk around the check.
  const homes = spellings(guards.home);
  const guarded = [guards.dataDir, ...guards.appDirs, ...homes.flatMap((dir) => CREDENTIALS.map((name) => join(dir, name)))]
    .flatMap(spellings);
  const held = [...new Set([resolve(folder), path])].some(
    (candidate) => candidate === "/" || homes.some((dir) => inside(dir, candidate)) ||
      guarded.some((dir) => inside(dir, candidate) || inside(candidate, dir)),
  );
  if (held) return refused(`the folder ${path} holds this computer's home folder or the app's own data`);
  return { ok: true, path, dev: stats.dev, ino: stats.ino };
}
