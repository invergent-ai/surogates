// The files in a folder that are also linked from elsewhere (spec, Section 4).
// A command in the sandbox can change a file outside the folder through a hard
// link that is already in it; neither the mounts nor a path check can see that.
// So the user is told before the folder is bound. The file helper refuses
// writes through such files itself: this is about commands.

import type { Dirent } from "node:fs";
import { lstat, readdir } from "node:fs/promises";
import { join, relative } from "node:path";

// ponytail: walked in the main process through fs/promises (libuv does the I/O off the
// main thread); move it to a worker if a large folder ever makes the window stall.
export const LINK_SCAN_MS = 5_000;
export const LINK_SCAN_ENTRIES = 200_000;
const EXAMPLES = 3;

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
  const deadline = performance.now() + (options.deadlineMs ?? LINK_SCAN_MS);
  const maxEntries = options.maxEntries ?? LINK_SCAN_ENTRIES;
  // By device and inode: how many links the file has, and those the walk met.
  const files = new Map<string, { nlink: number; paths: string[] }>();
  // Each folder still to look into, and whether it lies inside a .git folder.
  const folders = [{ dir: folder, inGit: false }];
  let entries = 0;
  let complete = true;
  walk: for (let next = folders.pop(); next !== undefined; next = folders.pop()) {
    const { dir, inGit } = next;
    let listed: Dirent[];
    try {
      listed = await readdir(dir, { withFileTypes: true });
    } catch {
      complete = false;
      continue;
    }
    // A git folder holds HEAD. Its objects/ is the object store only when it holds
    // no HEAD of its own: else it may be the git folder of a submodule named objects.
    const gitFolder = inGit && listed.some((entry) => entry.name === "HEAD");
    const regular: string[] = [];
    for (const entry of listed) {
      entries += 1;
      if (entries > maxEntries || performance.now() > deadline) {
        complete = false;
        break walk;
      }
      const path = join(dir, entry.name);
      const name = entry.name.toLowerCase();
      if (entry.isDirectory()) {
        const store = gitFolder && name === "objects"
          && await lstat(join(path, "HEAD")).then(() => false, (error: NodeJS.ErrnoException) => error.code === "ENOENT");
        if (name !== "node_modules" && !store) folders.push({ dir: path, inGit: inGit || name === ".git" });
      } else if (entry.isFile()) {
        regular.push(path);
      }
    }
    const stats = await Promise.allSettled(regular.map((path) => lstat(path)));
    stats.forEach((result, i) => {
      // A name that is not valid UTF-8 reads back with U+FFFD, and no path reaches it: the look missed that file.
      if (result.status !== "fulfilled") {
        complete = false;
        return;
      }
      if (!result.value.isFile() || result.value.nlink < 2) return;
      const key = `${result.value.dev}:${result.value.ino}`;
      const file = files.get(key) ?? { nlink: result.value.nlink, paths: [] };
      file.paths.push(regular[i] as string);
      files.set(key, file);
    });
  }
  const linked = [...files.values()].filter((file) => file.nlink > file.paths.length).flatMap((file) => file.paths);
  if (linked.length === 0 && complete) return null;
  return {
    count: linked.length,
    examples: linked.map((path) => relative(folder, path)).sort().slice(0, EXAMPLES),
    complete,
  };
}
