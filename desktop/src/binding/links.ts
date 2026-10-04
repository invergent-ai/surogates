// The files in a folder that are also linked from elsewhere (spec, Section 4).
// A command in the sandbox can change a file outside the folder through a hard
// link that is already in it; neither the mounts nor a path check can see that.
// So the user is told before the folder is bound. The file helper refuses
// writes through such files itself: this is about commands.

import { lstat, readdir } from "node:fs/promises";
import { join, relative } from "node:path";

// ponytail: walked in the main process through fs/promises, two requests at a time. A child
// process killed at the deadline is the robust upgrade: worker threads share the same libuv pool.
export const LINK_SCAN_MS = 5_000;
export const LINK_SCAN_ENTRIES = 200_000;
const EXAMPLES = 3;
// Requests in flight at once. One that hangs on a dead mount keeps its thread of libuv's 4,
// which every fs/promises call and dns.lookup in the process share.
const IN_FLIGHT = 2;

// A walk is under way, or still held by a request that never returned: the next does not start.
let walking = false;

export interface LinkSummary {
  // How many files in the folder share their data with a link outside it.
  count: number;
  // A few of them, relative to the folder, sorted.
  examples: string[];
  // False when the look stopped early (its deadline, its cap) or could not read a folder.
  complete: boolean;
}

export interface LinkScanOptions {
  deadlineMs?: number;
  maxEntries?: number;
}

// Links are not followed, and node_modules is skipped: pnpm hard-links it to its
// store, which would flag most pnpm projects and make the look slow. So are git's
// object stores, as the hook walk skips them: a local clone hard-links its objects to
// the repository it came from, and git never writes an object in place, so those
// links carry no write out of the folder. An inode counts only when some of its links
// lie outside the walk, so two links that are both in the folder are not "linked from
// elsewhere". Null: none found in a whole look.
export async function scanLinks(folder: string, options: LinkScanOptions = {}): Promise<LinkSummary | null> {
  if (walking) return { count: 0, examples: [], complete: false };
  const deadlineMs = options.deadlineMs ?? LINK_SCAN_MS;
  const deadline = performance.now() + deadlineMs;
  const maxEntries = options.maxEntries ?? LINK_SCAN_ENTRIES;
  // By device and inode: how many links the file has, and those the walk met.
  const files = new Map<string, { nlink: bigint; paths: string[] }>();
  let entries = 0;
  let complete = true;
  // Set when the look ends early: the walk then asks for nothing more.
  let stopped = false;
  const stop = () => {
    stopped = true;
    complete = false;
  };
  const late = () => {
    if (performance.now() > deadline) stop();
    return stopped;
  };

  const walk = async () => {
    // Each folder still to look into, and whether it lies inside a .git folder.
    const folders = [{ dir: folder, inGit: false }];
    for (let next = folders.pop(); next !== undefined; next = folders.pop()) {
      if (late()) return;
      const { dir, inGit } = next;
      let names: string[];
      try {
        // Names only, each type from its own lstat: Node looks up an entry the listing
        // gives no type for (XFS without ftype, some FUSE, NFSv3) on the main thread.
        names = await readdir(dir);
      } catch {
        complete = false;
        continue;
      }
      entries += names.length;
      if (entries > maxEntries) return stop();
      // A git folder holds HEAD. Its objects/ is the object store only when it holds
      // no HEAD of its own: else it may be the git folder of a submodule named objects.
      const gitFolder = inGit && names.includes("HEAD");
      let at = 0;
      const look = async () => {
        for (let name = names[at++]; name !== undefined && !late(); name = names[at++]) {
          // A name that is not valid UTF-8 reads back with U+FFFD, and no path reaches it:
          // the look misses it, and may meet a valid name it reads as in its place.
          if (name.includes("\uFFFD")) {
            complete = false;
            continue;
          }
          const path = join(dir, name);
          // Bigints: an inode number past 2^53 as a number could merge two files.
          const stats = await lstat(path, { bigint: true }).catch(() => null);
          // A file that went away as the walk passed: the look missed it.
          if (!stats) {
            complete = false;
          } else if (stats.isDirectory()) {
            const store = gitFolder && name === "objects"
              && await lstat(join(path, "HEAD")).then(() => false, (error: NodeJS.ErrnoException) => error.code === "ENOENT");
            if (name.toLowerCase() !== "node_modules" && !store) folders.push({ dir: path, inGit: inGit || name === ".git" });
          } else if (stats.isFile() && stats.nlink >= 2n) {
            const key = `${stats.dev}:${stats.ino}`;
            const file = files.get(key) ?? { nlink: stats.nlink, paths: [] };
            file.paths.push(path);
            files.set(key, file);
          }
        }
      };
      await Promise.all(Array.from({ length: IN_FLIGHT }, look));
    }
  };

  // The deadline also ends a request that never returns, as one on a hung mount would not.
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(() => {
      stop();
      resolve();
    }, deadlineMs);
  });
  walking = true;
  const walked = walk().catch(stop).finally(() => {
    walking = false;
  });
  try {
    await Promise.race([walked, timeout]);
  } finally {
    clearTimeout(timer);
  }
  const linked = [...files.values()].filter((file) => file.nlink > BigInt(file.paths.length)).flatMap((file) => file.paths);
  if (linked.length === 0 && complete) return null;
  return {
    count: linked.length,
    // Every path starts with the folder, so they sort as their relative forms do.
    examples: linked.sort().slice(0, EXAMPLES).map((path) => relative(folder, path)),
    complete,
  };
}
