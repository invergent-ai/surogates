// Which folders a chat may work on (spec, Section 4): checked when the user
// confirms one, and again by its tool host before anything runs there.

import { realpathSync, type Stats } from "node:fs";

import { identityOf } from "../files/identity.js";
import { inside, realpath } from "../files/paths.js";
import * as host from "../host-path.js";
import { hostStat } from "../host-stat.js";
import { FOLDER_BYTES, GLOB, isReserved } from "../hosts/policy.js";
import { forThisOs, notBuilt } from "../platform.js";
import { guestPathOf, hostPathOf, join, resolve } from "../session-path.js";
import { bootId } from "./boot-id.js";

const CREDENTIALS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];

// The folders of the guest's image that hold its tools (spec, Section 11, "Sessions in the
// guest"): a chat's folder is bound at its own path in the guest, over whatever the image
// has there. The image's other top-level folders are empty, or links into /usr, or the
// root's own mounts; /root is no user's to bind.
export const GUEST_SYSTEM = ["/etc", "/opt/venv", "/usr", "/var/cache", "/var/lib", "/var/log", "/var/spool"];

// This boot, read once (boot-id.ts); "" when it cannot be read.
export const BOOT_ID = bootId();

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

// How each OS names a folder as it resolves (FolderPaths): a chat's folder is named by its session's path, the guest's
// (session-path.ts), and opened by this computer's own. On Linux and macOS the two are one, resolved as the file host
// resolves a path, through each link on the way, which is noted. On Windows a folder is resolved as Windows resolves it,
// its final path (realpathSync.native, GetFinalPathNameByHandleW), its names in their case on disk; a folder on no
// drive, as a network share, has no session's path.
interface FolderPaths {
  /** This computer's own path of the folder at the session's path *folder*, as it resolves. */
  real(folder: string): string;
  /** A folder's resolved path *at*, this computer's own, as the session names it. */
  session(at: string): string;
  /**
   * The session's path *path* as spelled, and as the file system resolves it. Where it does not exist yet, or cannot be
   * read, the part that exists is still resolved: a guard that is not there yet is still where its links lead. *links*
   * gets each link the way to it goes through, by where that link is.
   */
  spellings(path: string, links: string[]): string[];
}

const POSIX_FOLDERS: FolderPaths = {
  real: (folder) => realpathSync(folder),
  session: (at) => at,
  spellings(path, links) {
    const plain = resolve(path);
    const through = new Map<string, string | null>();
    const { path: real } = realpath(plain, through);
    links.push(...through.keys());
    return real === plain ? [plain] : [plain, real];
  },
};

// The final path of this computer's *path*, or of the deepest folder of it that is there, with the rest as spelled.
function finalOf(path: string): string {
  try {
    return realpathSync.native(path);
  } catch {
    const parent = host.dirname(path);
    return parent === path ? path : host.join(finalOf(parent), host.basename(path));
  }
}

const WIN32_FOLDERS: FolderPaths = {
  real: (folder) => realpathSync.native(hostPathOf(folder)),
  session: (at) => guestPathOf(at),
  spellings(path) {
    const plain = resolve(path);
    let real: string;
    try {
      real = guestPathOf(finalOf(hostPathOf(plain)));
    } catch {
      return [plain]; // on no drive: no folder of this computer's is there
    }
    return real === plain ? [plain] : [plain, real];
  },
};

const FOLDERS = forThisOs<FolderPaths>({ linux: POSIX_FOLDERS, darwin: POSIX_FOLDERS, win32: WIN32_FOLDERS });
const spellings = (path: string, links: string[] = []): string[] => (FOLDERS ?? notBuilt("A chat's folder")).spellings(path, links);

// Whether *path* lies on a network volume, which a chat's folder may not be on Windows: a drive letter is mapped per
// logon session (Microsoft Learn, "Mapped drives are not available from an elevated command"), and on SMB every
// reparse tag but a symlink's is "processed on the server" (MS-FSCC, "Reparse Tags"), out of the walk's sight. Its
// path as Windows resolves it (libuv's realpath, GetFinalPathNameByHandleW) is a UNC path, \\server\share\..., for a
// share and for a mapped drive alike; a subst drive's is its real folder's. Linux and macOS bind any folder.
const ON_NETWORK = forThisOs<(path: string) => boolean>({
  linux: () => false,
  darwin: () => false,
  win32: (path) => realpathSync.native(path).startsWith("\\\\"),
});

/** Whether the folder at *path* is on a network volume, which this OS's file host cannot work in. */
export function onNetworkVolume(path: string): boolean {
  return (ON_NETWORK ?? (() => notBuilt("A chat's folder")))(path);
}

// The folder, named by its session's path *folder*, as it resolves, and its identity; or why no chat may work on it.
// Each guard is compared by the session's paths too, as the folder's own.
export function checkFolder(folder: string, guards: FolderGuards): FolderCheck {
  const paths = FOLDERS ?? notBuilt("A chat's folder");
  let at: string;
  let stats: Stats;
  // A name that is not valid UTF-8 reads back with U+FFFD, which opens nothing: such a folder is not there.
  try {
    at = paths.real(folder);
    stats = hostStat(at);
  } catch {
    return { ok: false, missing: true, message: `the folder ${folder} is not there` };
  }
  if (!stats.isDirectory()) return { ok: false, missing: true, message: `the folder ${folder} is not a folder` };
  const refused = (message: string): FolderCheck => ({ ok: false, missing: false, message });
  if (onNetworkVolume(at)) return refused(`the folder ${at} is on a network volume, which Surogate cannot work in on this computer`);
  let path: string;
  try {
    path = paths.session(at);
  } catch {
    return refused(`the folder ${at} is on no drive of this computer, which Surogate cannot work in`);
  }
  if (GLOB.test(path)) return refused(`this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${path}`);
  if (FOLDER_BYTES !== null && Buffer.byteLength(path) > FOLDER_BYTES) {
    return refused(`this computer cannot sandbox a folder whose path is longer than ${FOLDER_BYTES} bytes: ${path}`);
  }
  if (isReserved(at)) return refused(`the folder ${path} is inside one of this computer's system folders`);
  // One that is, holds or lies in a folder of the guest's tools would hide them from its commands.
  const tools = GUEST_SYSTEM.find((dir) => inside(path, dir) || inside(dir, path));
  if (tools) return refused(`the sandbox keeps its own tools in ${tools}, so the folder ${path} cannot be a chat's`);
  // A sandbox whose writable folder held the home folder, the app's own data, cache or
  // files or a credential folder would hand all of it to the agent. Each is
  // compared as spelled and as resolved: a link would otherwise walk around the check.
  // And every link on the way to one of them is guarded where it is: a folder that held such a
  // link would let a command point it elsewhere, and the app would follow it there.
  const links: string[] = [];
  const homes = spellings(guestPathOf(guards.home), links);
  const guarded = [guards.dataDir, guards.cacheDir, ...guards.appDirs].map(guestPathOf)
    .concat(homes.flatMap((dir) => CREDENTIALS.map((name) => join(dir, name))))
    .flatMap((dir) => spellings(dir, links));
  const held = [...new Set([resolve(folder), path])].some(
    (candidate) => candidate === "/" || homes.some((dir) => inside(dir, candidate)) ||
      guarded.some((dir) => inside(dir, candidate) || inside(candidate, dir)) || links.some((link) => inside(link, candidate)),
  );
  if (held) return refused(`the folder ${path} holds this computer's home folder or the app's own data`);
  return { ok: true, path, ...identityOf(stats) };
}
