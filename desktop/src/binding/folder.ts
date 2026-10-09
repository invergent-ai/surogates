// Which folders a chat may work on (spec, Section 4): checked when the user
// confirms one, and again by its tool host before anything runs there.

import { readFileSync, realpathSync, type Stats, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { inside, realpath } from "../files/paths.js";
import { GLOB, isReserved } from "../hosts/policy.js";

const CREDENTIALS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];

// The folders of the guest's image that hold its tools (spec, Section 11, "Sessions in the
// guest"): a chat's folder is bound at its own path in the guest, over whatever the image
// has there. The image's other top-level folders are empty, or links into /usr, or the
// root's own mounts; /root is no user's to bind.
export const GUEST_SYSTEM = ["/etc", "/opt/venv", "/usr", "/var/cache", "/var/lib", "/var/log", "/var/spool"];

// This boot, read once; "" when it cannot be read. A folder's st_dev belongs to its mount and
// can change at a reboot (btrfs subvolumes, ZFS, NFS and SMB, FUSE, several NVMe drives).
export const BOOT_ID = (() => {
  try {
    return readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim();
  } catch {
    return "";
  }
})();

// Whether *found* is the folder confirmed as *expect*. A reboot can renumber the folder's
// mount: after one, only the inode is compared. A boot id that could not be read counts as this boot.
export function confirmedFolder(expect: { dev: number; ino: number; boot?: string }, found: { dev: number; ino: number }): boolean {
  const rebooted = Boolean(expect.boot) && BOOT_ID !== "" && expect.boot !== BOOT_ID;
  return found.ino === expect.ino && (rebooted || found.dev === expect.dev);
}

// What a chat's folder must keep clear of: the home folder, the app's own data,
// cache and files (which run outside the sandbox), and the credential folders.
export interface FolderGuards {
  home: string;
  dataDir: string;
  // The app's own cache folder, <cache home>/surogate, where it downloads an update. The app,
  // which is not confined, moves and removes what is there: nothing a chat's commands can write
  // may hold it or lie in it, or a link they put there would be the app's to follow.
  cacheDir: string;
  appDirs: string[];
}

export type FolderCheck =
  | { ok: true; path: string; dev: number; ino: number }
  // missing: the folder is not there, or is not a folder; otherwise it may never be a chat's.
  | { ok: false; missing: boolean; message: string };

// A path as spelled, and as the file system resolves it. Where it does not exist yet, or cannot be
// read, the part that exists is still resolved: a guard that is not there yet is still where its links lead.
// *links* gets each link the way to it goes through, by where that link is.
function spellings(path: string, links: string[] = []): string[] {
  const plain = resolve(path);
  const through = new Map<string, string | null>();
  const { path: real } = realpath(plain, through);
  links.push(...through.keys());
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
  // One that is, holds or lies in a folder of the guest's tools would hide them from its commands.
  const tools = GUEST_SYSTEM.find((dir) => inside(path, dir) || inside(dir, path));
  if (tools) return refused(`the sandbox keeps its own tools in ${tools}, so the folder ${path} cannot be a chat's`);
  // A sandbox whose writable folder held the home folder, the app's own data, cache or
  // files or a credential folder would hand all of it to the agent. Each is
  // compared as spelled and as resolved: a link would otherwise walk around the check.
  // And every link on the way to one of them is guarded where it is: a folder that held such a
  // link would let a command point it elsewhere, and the app would follow it there.
  const links: string[] = [];
  const homes = spellings(guards.home, links);
  const guarded = [guards.dataDir, guards.cacheDir, ...guards.appDirs, ...homes.flatMap((dir) => CREDENTIALS.map((name) => join(dir, name)))]
    .flatMap((dir) => spellings(dir, links));
  const held = [...new Set([resolve(folder), path])].some(
    (candidate) => candidate === "/" || homes.some((dir) => inside(dir, candidate)) ||
      guarded.some((dir) => inside(dir, candidate) || inside(candidate, dir)) || links.some((link) => inside(link, candidate)),
  );
  if (held) return refused(`the folder ${path} holds this computer's home folder or the app's own data`);
  return { ok: true, path, dev: stats.dev, ino: stats.ino };
}
