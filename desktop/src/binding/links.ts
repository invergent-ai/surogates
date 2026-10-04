// The files in a folder that are also linked from elsewhere (spec, Section 4).
// A command in the sandbox can change a file outside the folder through a hard
// link that is already in it; neither the mounts nor a path check can see that.
// So the user is told before the folder is bound. The file helper refuses
// writes through such files itself: this is about commands.

import { lstat, opendir } from "node:fs/promises";
import { join, relative } from "node:path";

// ponytail: walked in the main process through fs/promises, a batch per turn of the
// event loop; move it to a worker if the final sort of very many links ever stalls the window.
export const LINK_SCAN_MS = 5_000;
export const LINK_SCAN_ENTRIES = 200_000;
const EXAMPLES = 3;
// Entries read, and files looked at, per request: a large folder never holds the process up.
const BATCH = 256;

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
      const subfolders: string[] = [];
      const regular: string[] = [];
      let head = false;
      try {
        for await (const entry of await opendir(dir, { bufferSize: BATCH })) {
          if (stopped) return;
          entries += 1;
          if (entries > maxEntries) return stop();
          // A name that is not valid UTF-8 reads back with U+FFFD, and no path reaches it:
          // the look misses it, and may meet a valid name it reads as in its place.
          if (entry.name.includes("\uFFFD")) {
            complete = false;
            continue;
          }
          if (entry.name === "HEAD") head = true;
          if (entry.isDirectory()) subfolders.push(entry.name);
          else if (entry.isFile()) regular.push(join(dir, entry.name));
        }
      } catch {
        complete = false;
        continue;
      }
      // A git folder holds HEAD. Its objects/ is the object store only when it holds
      // no HEAD of its own: else it may be the git folder of a submodule named objects.
      for (const name of subfolders) {
        const path = join(dir, name);
        const store = inGit && head && name === "objects"
          && await lstat(join(path, "HEAD")).then(() => false, (error: NodeJS.ErrnoException) => error.code === "ENOENT");
        if (name.toLowerCase() !== "node_modules" && !store) folders.push({ dir: path, inGit: inGit || name === ".git" });
      }
      for (let start = 0; start < regular.length; start += BATCH) {
        if (late()) return;
        const batch = regular.slice(start, start + BATCH);
        // Bigints: an inode number past 2^53 as a number could merge two files.
        const stats = await Promise.allSettled(batch.map((path) => lstat(path, { bigint: true })));
        stats.forEach((result, i) => {
          // A file that went away as the walk passed: the look missed it.
          if (result.status !== "fulfilled") {
            complete = false;
            return;
          }
          if (!result.value.isFile() || result.value.nlink < 2n) return;
          const key = `${result.value.dev}:${result.value.ino}`;
          const file = files.get(key) ?? { nlink: result.value.nlink, paths: [] };
          file.paths.push(batch[i] as string);
          files.set(key, file);
        });
      }
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
  try {
    await Promise.race([walk(), timeout]);
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
