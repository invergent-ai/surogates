// A folder too large for its threads to have copies of their own on this computer (spec, Section 13, "The user's
// computer"): one whose first copy the history's bound cut (history/copies.ts, CUT_MS). Each try would take the minutes
// of that bound again, so from the first cut on the folder is refused at once, for every thread on it, and the app's
// data remembers it across the app's restarts: <data>/history/<key>.too-large.json, beside its place and outside it,
// where the guest writes nothing. By the folder's identity, as its place's record names one (history/place.ts), and
// with how many of its files its history would take then: it is tried again once it holds another number of them, or
// when its person asks.

import { randomUUID } from "node:crypto";
import { lstat, readdir, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";

import { confirmedFolder } from "../binding/folder.js";
import { dirname, join } from "../host-path.js";
import type { Place } from "../vm/manager.js";
import { digitsOf, keyOf, numberOf } from "./place.js";

// How long a count of a folder's files may take: a dead mount, or a stalled disk, never answers.
export const COUNT_MS = 5_000;

// What a folder's history leaves out of it (surogates/sandbox/local_history.py, LOCAL_EXCLUDES), as it leaves it out: a
// name that ends in "/" is a folder's anywhere, one that starts with "/" a folder's at the top, and any other a file's or
// a folder's anywhere. So a count changes as the files history takes come and go, and not as a build, an editor or git
// writes what it never takes. Kept in step with the history's by hand: one out of step tries a folder sooner, or later.
const EXCLUDES = [
  "node_modules/", "dist/", "build/", ".env", ".env.*", ".env.local", ".env.*.local", "__pycache__/", "*.pyc", "*.pyo", ".DS_Store",
  ".cache/", ".next/", ".nuxt/", "coverage/", ".pytest_cache/", ".venv/", "venv/", ".git/", "~$*", ".~lock.*#", "~WRL*.tmp",
  "~WRD*.tmp", "*.tmp", ".goutputstream-*", "*.crdownload", "*.part", "*~", ".#*", "*.swp", "Thumbs.db", "desktop.ini", "._*",
  "/_whiteboard/", "/.surogates-results/", "/.threads/",
];
// One name's glob as a pattern: its * and ? never cross a "/".
const glob = (name: string) => [...name].map((c) => (c === "*" ? "[^/]*" : c === "?" ? "[^/]" : c.replace(/[\\^$.|+()[\]{}/-]/g, "\\$&"))).join("");
// The excludes as one pattern over a path from the folder's top, a folder's ending in "/".
const EXCLUDED = new RegExp(EXCLUDES.map((pattern) => {
  if (pattern.startsWith("/")) return `^${glob(pattern.slice(1, -1))}/`;
  return pattern.endsWith("/") ? `(?:^|/)${glob(pattern.slice(0, -1))}/` : `(?:^|/)${glob(pattern)}(?:/|$)`;
}).join("|"));

// How many files of *folder* its history would take: each that is no folder, in each folder the excludes leave in, no
// link followed, as the history counts them before a first copy (local_history.py, _off). What cannot be listed or
// looked at counts as nothing, as there. Never rejects.
async function walked(folder: string): Promise<number> {
  let count = 0;
  const folders = [""];
  for (let inside = folders.pop(); inside !== undefined; inside = folders.pop()) {
    for (const name of await readdir(join(folder, inside)).catch((): string[] => [])) {
      const path = inside === "" ? name : `${inside}/${name}`;
      const found = await lstat(join(folder, path)).catch(() => null);
      if (found?.isDirectory()) {
        if (!EXCLUDED.test(`${path}/`)) folders.push(path);
      } else if (found && !EXCLUDED.test(path)) {
        count += 1;
      }
    }
  }
  return count;
}

// Each folder's count while it runs, so one on a mount that never answers holds one of libuv's threads, however often it is asked.
const counting = new Map<string, Promise<number>>();

// How many files of *folder* its history would take, or null where that is not told within *ms*.
async function filesIn(folder: string, ms: number): Promise<number | null> {
  let walk = counting.get(folder);
  if (!walk) {
    const started = walked(folder);
    walk = started;
    counting.set(folder, started);
    const done = () => void (counting.get(folder) === started && counting.delete(folder));
    started.then(done, done);
  }
  let timer: NodeJS.Timeout | undefined;
  const late = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), ms).unref();
  });
  return Promise.race([walk, late]).finally(() => clearTimeout(timer));
}

// The record of *place*'s folder as too large: beside the place's own record, outside the place.
const recordOf = (place: Place) => join(dirname(place.history), `${place.key}.too-large.json`);
// The record's path by the folder's key, in the app's data *dataDir* by its real path, as a place's is.
const recordAt = async (dataDir: string, folder: string) => join(await realpath(dataDir), "history", `${keyOf(folder)}.too-large.json`);

/**
 * Whether *place*'s folder is refused as too large to copy in time: its record names that folder, and the folder holds
 * as many files its history takes as it did then, or whether it does could not be told, then or now. One that holds
 * another number has its record go, and is tried. Never rejects.
 */
export async function tooLargeFor(place: Place, ms = COUNT_MS): Promise<boolean> {
  const said = await readFile(recordOf(place), "utf8").then(recorded, () => null);
  const { real } = place;
  // One folder's, whichever of the two was read in an earlier boot, as a place's record is read.
  if (said === null || said.path !== real.path || !(confirmedFolder(said, real) || confirmedFolder(real, said))) return false;
  if (said.files === null) return true;
  const files = await filesIn(real.path, ms);
  if (files === null || files === said.files) return true;
  await rm(recordOf(place), { force: true }).catch(() => {});
  return false;
}

/** Remember *place*'s folder as too large to copy in time, with how many of its files its history takes now. Never rejects. */
export async function rememberTooLarge(place: Place, ms = COUNT_MS): Promise<void> {
  const { path, dev, ino, boot } = place.real;
  const files = await filesIn(path, ms);
  const record = recordOf(place);
  // Whole or not at all.
  const temp = `${record}.${randomUUID()}`;
  try {
    await writeFile(temp, JSON.stringify({ path, dev: digitsOf(dev), ino: digitsOf(ino), boot, files }), { mode: 0o600 });
    await rename(temp, record);
  } catch {
    await rm(temp, { force: true }).catch(() => {});
  }
}

/**
 * The folder at *folder*'s path, in the app's data *dataDir*, is no longer remembered as too large: it is tried at the
 * next operation. By the path's key alone, before its place is found. Never rejects.
 */
export async function forgetTooLarge(dataDir: string, folder: string): Promise<void> {
  // Where the app's data is not there, nothing of it is remembered.
  await recordAt(dataDir, folder).then((record) => rm(record, { force: true })).catch(() => {});
}

// A record as it was written, or null for one that names no folder. Its count is as it lies: one that is none equals
// no count, and its folder is tried.
function recorded(text: string): { path: string; dev: number; ino: number; boot: string; files: unknown } | null {
  try {
    const { path, dev, ino, boot, files } = JSON.parse(text) as Record<string, unknown>;
    const [device, inode] = [numberOf(dev), numberOf(ino)];
    if (typeof path !== "string" || typeof boot !== "string" || device === null || inode === null) return null;
    return { path, dev: device, ino: inode, boot, files };
  } catch {
    return null;
  }
}
