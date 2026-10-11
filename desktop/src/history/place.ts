// Where a folder's history and its threads' copies live on this computer (spec, Section 13, "The
// model"): in the app's data, never in the folder.
//
//   <data>/history/<key>/          the place, shared whole with the guest's root for its git:
//       history.git/               the folder's history
//       clones/<thread>/           a thread's own repository
//       threads/<thread>/          the thread's copy, which is also its root's share
//   <data>/history/<key>.json      which folder it is the history of: its path, and its device's
//                                  and its file's numbers as digits
//   <data>/history/<key>.too-large.json  a folder whose first copy the history's bound cut, and how
//                                  many of its files history took then (history/too-large.ts)
//   <data>/landings/<key>/         what a landing keeps of the files it replaces, until it is recorded
//
// <key> is the checkpoint manager's (surogates/tools/utils/checkpoint_manager.py,
// _shadow_repo_path): the first 16 hex digits of the SHA-256 of the folder's path. It is made
// here, from the path a binding holds, so nothing a server or a thread says names a place; and a
// copy is named by its thread, which is a session's id or nothing. The record and the kept files
// are outside the place: the guest, which is given the place, can write neither.
//
// <data> is the app's data by its real path. The sandbox shares a place, and a landing reads a
// copy, only by the path each really has (vm/manager.ts, placed), so an app whose data is reached
// through a link has its places where the link leads: resolved here, once for every path of a place.
//
// Another folder at the same path starts a new history: the place that was there, its record and
// what its landings kept are renamed <key>.was-<time>, each where it lies. Only for the folder that
// is at the path now, and only once the sandbox has let the place go: a thread bound to a folder
// that is no longer the one at its path sets nothing aside, has no place made for it, and is given
// none but the one that is its own folder's history. What is set aside under a key takes 10 GiB of
// the disk at most: past that the oldest goes, and never the newest.

import { createHash } from "node:crypto";
import { closeSync, fsyncSync, lstatSync, mkdirSync, openSync, readdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import { chmod, lstat, readdir, realpath, rename, rm } from "node:fs/promises";
import { join, sep } from "node:path";

import { confirmedFolder } from "../binding/folder.js";
import { strerror } from "../files/answers.js";
import { PLACE_KEY } from "../guest/protocol.js";
import type { BoundFolder } from "../hosts/tool-hosts.js";
import { THREAD } from "../vm/history.js";
import type { Folder, Place } from "../vm/manager.js";

// How long a look at a folder may take: a dead network or FUSE mount never answers.
const LOOK_MS = 5_000;

const NOT_THE_APPS = "This folder's history is not a folder of the app's own";
// How often one ask looks again at a place that was changed while it waited for the folder's look or for the
// sandbox: another thread's ask changes it once, and then it is the folder's own.
const TURNS = 8;
// What the places set aside under one key take of the disk at most, each with its record and what its landings kept,
// as the copies a place sets aside do (surogates/sandbox/local_history.py, _ASIDE_BYTES).
export const ASIDE_BYTES = 10 * 1024 ** 3;
// A name of what was set aside under a key: its history, its record, or what its landings kept; or one that is going.
const ASIDE = /^([0-9a-f]{16})\.was-([0-9]+)(\.json|\.gone)?$/;

// The folder is not the one its thread was bound to: another folder, a link, or nothing is at its path now.
export class FolderReplaced extends Error {}

// What a look at a folder found: its identity, and its real path.
export interface Looked {
  found: { directory: boolean; dev: number; ino: number } | null;
  real: string | null;
}

export interface PlaceOptions {
  /**
   * Everything of this computer's that works in *place* lets it go, the sandbox last (VmManager.unplace):
   * settled once it has. A place is renamed aside only after, and not at all where this rejects. *store*
   * is the inode of the place's folder in the app's data as this ask read it: the folder it renames, if
   * that is still the one there, and no other made at its path since.
   */
  letGo(place: Place, store: number): Promise<unknown>;
  // How a folder is looked at, and how long the look may take; node's lstat and LOOK_MS unless a test says. A look
  // rejects, with why, where the folder could not be read.
  look?(path: string): Promise<Looked>;
  lookMs?: number;
  // ASIDE_BYTES unless a test says.
  asideBytes?: number;
  // Told, once this ask has set a place aside, of the bound's run on what is set aside under the folder's path, which
  // goes on behind the ask's answer: it settles once that has run.
  bounded?(run: Promise<void>): void;
}

export interface Placed {
  place: Place;
  // Another folder's place was at this folder's key: the place as the sandbox was asked to let it go, where
  // it is kept now, and where what its landings kept is, if they kept anything.
  aside?: { place: Place; history: string; kept: string | null };
}

export const keyOf = (folder: string): string => createHash("sha256").update(folder).digest("hex").slice(0, 16);

/**
 * What a folder's landings keep, by its name in <data>/landings: the folder's key; or, once another folder took the
 * folder's path and its place was set aside with what its landings kept, that key and when.
 */
export const KEPT_NAME = /^([0-9a-f]{16})(?:\.was-[0-9]+)?$/;

/** *thread*'s copy in *place*: where its tools and commands work. */
export function copyOf(place: Place, thread: string): string {
  // A name that is no session's id could lead out of the place, or to another thread's copy.
  if (!THREAD.test(thread)) throw new Error(`${JSON.stringify(thread).slice(0, 64)} is no thread's id, so it names no copy of a folder`);
  return join(place.history, "threads", thread);
}

/** What the landings of *place* keep of the files they replace, in the app's data *dataDir*, outside the place. */
export function keptOf(dataDir: string, place: Place): string {
  const data = realpathSync(dataDir);
  // Only for a place of this data's own: a key that is none, or a history kept elsewhere, names no kept files here.
  if (!PLACE_KEY.test(place.key) || place.history !== join(data, "history", place.key)) throw new Error(NOT_THE_APPS);
  const landings = join(data, "landings");
  const kept = join(landings, place.key);
  if (own(landings)) own(kept);
  return kept;
}

interface Recorded {
  path: string;
  dev: number;
  ino: number;
  boot: string;
}

/**
 * A device's or a file's number as a record holds it: its digits, whole. A binding keeps each as the number stat gave,
 * past 2^53 too, where SMB, CIFS and overlayfs give them (journal/bindings.ts), and a record keeps the same number:
 * none is written or read as a fraction, which would round it. Null for what is no whole number.
 */
export const digitsOf = (value: number): string | null => (Number.isInteger(value) ? BigInt(value).toString() : null);
const DIGITS = /^-?[0-9]{1,309}$/;

/**
 * The number a record's digits are, where they are those of a number a binding can hold, spelt as digitsOf spells
 * it: it began as that number, so Number() gives it back exactly. Null for anything else.
 */
export function numberOf(digits: unknown): number | null {
  if (typeof digits !== "string" || !DIGITS.test(digits)) return null;
  const value = Number(BigInt(digits));
  return digitsOf(value) === digits ? value : null;
}

// Whether the folder at *path* is *other*, or lies in it: both real paths.
const within = (path: string, other: string) => path === other || path.startsWith(other.endsWith(sep) ? other : other + sep);

// A folder of the places' is there, and is a folder of its own: a link in its stead would share whatever it leads to.
function own(path: string): boolean {
  try {
    if (!lstatSync(path).isDirectory()) throw new Error(NOT_THE_APPS);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}

// A record as it lies, or null where there is none.
function textOf(record: string): string | null {
  try {
    return readFileSync(record, "utf8");
  } catch {
    return null;
  }
}

// Which folder a record says its place is the history of; null for one that cannot be read.
function recordedIn(text: string | null): Recorded | null {
  try {
    const record = (text === null ? null : JSON.parse(text)) as { path?: unknown; dev?: unknown; ino?: unknown; boot?: unknown } | null;
    if (typeof record !== "object" || record === null || typeof record.path !== "string" || typeof record.boot !== "string") return null;
    const [dev, ino] = [numberOf(record.dev), numberOf(record.ino)];
    return dev === null || ino === null ? null : { path: record.path, dev, ino, boot: record.boot };
  } catch {
    return null;
  }
}

/**
 * The folder whose landings keep what they replaced in <data>/landings/*name* (KEPT_NAME), as its place's record names
 * it: its path, and its device's and its file's numbers when it was recorded. Null where that record is not there,
 * cannot be read, or names a folder of another key. Nothing at the folder's path is looked at: whether the folder there
 * is the one recorded, the host that would hold it says (hosts/start.ts).
 */
export function recordedFolder(dataDir: string, name: string): Folder | null {
  const key = KEPT_NAME.exec(name)?.[1];
  if (key === undefined) return null;
  let data: string;
  try {
    data = realpathSync(dataDir);
  } catch {
    return null;
  }
  const recorded = recordedIn(textOf(join(data, "history", `${name}.json`)));
  return recorded === null || keyOf(recorded.path) !== key ? null : recorded;
}

// Whole or not at all, and on disk before any history is made in the place.
function write(record: string, recorded: { path: string; dev: string; ino: string; boot: string }): void {
  const temp = `${record}.${process.pid}`;
  const fd = openSync(temp, "w", 0o600);
  try {
    writeFileSync(fd, JSON.stringify(recorded));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temp, record);
}

// Each folder's look while it waits, so one that never answers holds one of libuv's threads, however often it is asked.
const looking = new Map<string, Promise<Looked>>();

// A look's errors that say nothing is at the path that could be the folder: no name, a file on its way, or links that lead nowhere.
const NOTHING_THERE = new Set(["ENOENT", "ENOTDIR", "ELOOP"]);

// What is at *path*. Rejects, in words for a person, where it could not be read: a network folder that is down, or
// one whose way is shut to this user, may be the folder that was bound or not.
async function seen(path: string): Promise<Looked> {
  try {
    // lstat: a link at its path is never the folder.
    const found = await lstat(path);
    return { found: { directory: found.isDirectory(), dev: found.dev, ino: found.ino }, real: await realpath(path) };
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    if (code !== undefined && NOTHING_THERE.has(code)) return { found: null, real: null };
    const why = strerror(code) ?? (error instanceof Error ? error.message : String(error));
    throw new Error(`The folder ${path} could not be read (${why}), so whether it is the one this thread was bound to cannot be told. Once it can be read, ask again`);
  }
}

// Whether the folder *bound* is the one at its path now, as the sandbox will take it: a folder, the one that
// was bound, at its own real path. Throws where the path did not answer in time, or could not be read: that says neither.
async function live(bound: BoundFolder, options: PlaceOptions): Promise<boolean> {
  let look = looking.get(bound.folder);
  if (!look) {
    look = (options.look ?? seen)(bound.folder);
    looking.set(bound.folder, look);
    const done = () => void looking.delete(bound.folder);
    look.then(done, done);
  }
  const ms = options.lookMs ?? LOOK_MS;
  let timer: NodeJS.Timeout | undefined;
  const looked = await Promise.race([
    look,
    new Promise<"late">((resolve) => {
      timer = setTimeout(() => resolve("late"), ms).unref();
    }),
  ]).finally(() => clearTimeout(timer));
  if (looked === "late") throw new Error(`The folder ${bound.folder} did not answer within ${ms / 1000} s`);
  return looked.found !== null && looked.found.directory && confirmedFolder(bound, looked.found) && looked.real === bound.folder;
}

// A name beside *paths* that none of them has yet with it, <path>.was-<time>, its time *after* or later: what is set
// aside is stamped in the order it was, whatever the clock says, as the newest is told by its stamp.
function stamped(paths: string[], after: number): string {
  for (let time = Math.max(Date.now(), after); ; time += 1) {
    const stamp = `.was-${time}`;
    if (paths.every((path) => lstatSync(`${path}${stamp}`, { throwIfNoEntry: false }) === undefined)) return stamp;
  }
}

// The latest stamp of what is set aside under *key* in the app's data *data*, kept or going; 0 where there is none.
function latestAside(data: string, key: string): number {
  let latest = 0;
  for (const folder of [join(data, "history"), join(data, "landings")]) {
    let names: string[] = [];
    try {
      names = readdirSync(folder);
    } catch {
      // Nothing was ever kept there.
    }
    for (const named of names.map((name) => ASIDE.exec(name))) if (named?.[1] === key) latest = Math.max(latest, Number(named[2]));
  }
  return latest;
}

// What *path* and all it holds take of the disk, in bytes, no link followed: what may not be looked at, or listed, counts as nothing more.
async function onDisk(path: string): Promise<number> {
  const found = await lstat(path).catch(() => null);
  if (found === null) return 0;
  if (!found.isDirectory()) return found.blocks * 512;
  let taken = found.blocks * 512;
  for (const name of await readdir(path).catch(() => [])) taken += await onDisk(join(path, name));
  return taken;
}

// Give each folder under *path*, *path* among them, all of its owner's rights, no link followed: the app's own, let go,
// so that a folder a thread's command made one nobody may change, or list, keeps nothing from going.
async function changeable(path: string): Promise<void> {
  const found = await lstat(path);
  if (!found.isDirectory()) return;
  await chmod(path, (found.mode & 0o7777) | 0o700);
  for (const name of await readdir(path)) await changeable(join(path, name)).catch(() => {});
}

/**
 * Let go of the oldest of what was set aside under *key* in the app's data *data*, until what is kept takes *most*
 * bytes of the disk at most: each place set aside goes with its record and its landings' folder, as one, and the
 * newest never, however much it holds. Nor one whose landings still keep anything: a landing cut short there may
 * keep the only copy of a file of the user's it replaced, which no recovery could put back once it went. It counts,
 * and is passed over; where none other can go, none does. The oldest is the one set aside first, by its stamp, which
 * goes in the order they were set aside in (stamped). Each is renamed to go first, where it lies (<name>.gone), its
 * record removed, and then it is, its folders made its owner's to change first: cut anywhere, none is kept with part of
 * it gone, and the next set-aside removes what is left. None of it is the sandbox's: a place is set aside only once the
 * sandbox has let it go, and none of it is anybody's but the app's.
 */
async function keptWithin(data: string, key: string, most: number): Promise<void> {
  const times = new Map<string, string[]>();
  const going: string[] = [];
  for (const folder of [join(data, "history"), join(data, "landings")]) {
    for (const name of await readdir(folder).catch(() => [])) {
      const named = ASIDE.exec(name);
      if (named?.[1] !== key) continue;
      if (named[3] === ".gone") going.push(join(folder, name));
      else times.set(named[2]!, [...(times.get(named[2]!) ?? []), join(folder, name)]);
    }
  }
  const oldest = [...times.keys()].sort((a, b) => Number(a) - Number(b));
  const taken = new Map<string, number>();
  for (const time of oldest) taken.set(time, (await Promise.all(times.get(time)!.map(onDisk))).reduce((sum, bytes) => sum + bytes, 0));
  let held = [...taken.values()].reduce((sum, bytes) => sum + bytes, 0);
  for (const time of oldest.slice(0, -1)) {
    if (held <= most) break;
    // Anything at all in its landings' folder: a step's record, a file it kept, or what a put-back has not told yet.
    const landings = join(data, "landings", `${key}.was-${time}`);
    if (times.get(time)!.includes(landings) && (await readdir(landings)).length > 0) continue;
    // Its folders first, then its record: none is left set aside with no record of which folder's it was.
    const [folders, records] = [times.get(time)!.filter((path) => !path.endsWith(".json")), times.get(time)!.filter((path) => path.endsWith(".json"))];
    for (const path of folders) {
      await rename(path, `${path}.gone`);
      going.push(`${path}.gone`);
    }
    for (const path of records) await rm(path, { force: true });
    held -= taken.get(time)!;
  }
  // Each on its own: one that cannot be removed whole now keeps none after it, and is tried again at the next set-aside.
  for (const path of going) {
    await changeable(path).catch(() => {});
    await rm(path, { recursive: true, force: true }).catch(() => {});
  }
}

// Each key's bound on what is set aside under it, one run after another, behind the asks that set places aside.
const bounding = new Map<string, Promise<void>>();

// The bound on what is set aside under *key* run once those asked before it have: never on an ask's way. Never rejects.
function keepWithin(data: string, key: string, most: number): Promise<void> {
  const at = `${data}\0${key}`;
  const run = (bounding.get(at) ?? Promise.resolve()).then(() => keptWithin(data, key, most)).catch(() => {});
  bounding.set(at, run);
  void run.then(() => {
    if (bounding.get(at) === run) bounding.delete(at);
  });
  return run;
}

/**
 * The place of the folder *bound* in the app's data *dataDir*, made when there is none: one for
 * every thread on the folder, whichever project each is of. A place that is its folder's own is
 * found by its record alone, with no look at the folder: whether the folder is still there is the
 * sandbox's to say, which looks before it adds a place (vm/manager.ts, placed).
 *
 * A place that is the history of another folder, one replaced at the same path since, is kept
 * aside and a new one made: its commits are not this folder's. So is one whose record cannot be
 * read. A place is made, and one set aside, only for the folder that is at its path now, and one
 * is set aside only once *options.letGo* has settled for it: the answer then says which place was
 * renamed, and where it and what its landings kept are now. For a thread bound to a folder that is
 * no longer the one at its path it rejects with FolderReplaced, and nothing is made or renamed.
 *
 * One ask makes at most one place and sets aside at most one, whatever it then reads: a place it
 * made that is not found to be this folder's is left as it is, and the ask rejects. So does one
 * whose place was changed each time it waited. Any rejection but FolderReplaced says why this
 * folder has no place, in words for a person.
 */
export async function placeOf(dataDir: string, bound: BoundFolder, options: PlaceOptions): Promise<Placed> {
  const key = keyOf(bound.folder);
  const [dev, ino] = [digitsOf(bound.dev), digitsOf(bound.ino)];
  // No folder has such numbers, and no record could say that a place is its history.
  if (dev === null || ino === null) throw new Error(`The folder ${bound.folder} was bound with no device and file number of a folder's, so it has no history`);
  mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  const data = realpathSync(dataDir);
  // Apart from the folder, both ways: in a folder that held the app's data, the place's own files would
  // be the folder's, recorded and landed as such; in one that lay in it, a thread would work in the app's data.
  if (within(data, bound.folder) || within(bound.folder, data)) {
    throw new Error(`The folder ${bound.folder} holds the app's own data, or lies in it, so its history cannot be kept apart from it`);
  }
  const histories = join(data, "history");
  const history = join(histories, key);
  const record = join(histories, `${key}.json`);
  const kept = join(data, "landings", key);
  const real: Folder = { path: bound.folder, dev: bound.dev, ino: bound.ino, boot: bound.boot };
  const place: Place = { key, history, real };
  // The place as it lies: its folder, its record as text, which folder that names, and whether it is this one.
  const read = () => {
    const there = own(histories) && own(history) ? lstatSync(history).ino : null;
    const text = textOf(record);
    const recorded = recordedIn(text);
    // One folder's, whichever of the two was read in an earlier boot: only an identity read in this one says which device it is on.
    const same = recorded !== null && recorded.path === bound.folder && (confirmedFolder(recorded, bound) || confirmedFolder(bound, recorded));
    return { there, text, recorded, found: there !== null && same, lost: there === null && same };
  };
  for (let turn = 0; turn < TURNS; turn += 1) {
    const { there, text, recorded, found, lost } = read();
    if (found) return { place };
    if (!(await live(bound, options))) throw new FolderReplaced(`The folder ${bound.folder} is not the one this thread was bound to`);
    // The place as the sandbox may hold it: the recorded folder's, or this folder's where no record says.
    const was: Place = { key, history, real: recorded ? { path: recorded.path, dev: recorded.dev, ino: recorded.ino, boot: recorded.boot } : real };
    if (there !== null) await options.letGo(was, there);
    // Looked at again: while the folder was looked at and the sandbox let the place go, another thread of the
    // folder may have done all of this. From here to the place's making nothing waits.
    if ((lstatSync(history, { throwIfNoEntry: false })?.ino ?? null) !== there || textOf(record) !== text) continue;
    const stamp = stamped([history, kept], latestAside(data, key) + 1);
    let aside: Placed["aside"];
    let setAside = false;
    if (there !== null) {
      renameSync(history, `${history}${stamp}`);
      if (text !== null) renameSync(record, `${history}${stamp}.json`);
      aside = { place: was, history: `${history}${stamp}`, kept: null };
      setAside = true;
    }
    // What the landings of a place before this one kept is that folder's, and goes aside with it. Where only the
    // place's own folder was lost, its record still names this folder, and what its landings kept is its own.
    if (!lost && lstatSync(kept, { throwIfNoEntry: false }) !== undefined) {
      renameSync(kept, `${kept}${stamp}`);
      if (aside) aside.kept = `${kept}${stamp}`;
      setAside = true;
    }
    if (there === null && !own(histories)) mkdirSync(histories, { mode: 0o700 });
    // The record first: cut between the two, the place is made at the next ask, and none is ever found without its record.
    write(record, { path: bound.folder, dev, ino, boot: bound.boot });
    mkdirSync(history, { mode: 0o700 });
    // Read as the next ask will read it. This ask has made its one place: one that is not found to be this
    // folder's is left as it is, and nothing more is renamed for it.
    if (read().found) {
      // Upkeep, behind the answer: the place is made whatever comes of it, and what could not go goes at the next set-aside.
      if (setAside) options.bounded?.(keepWithin(data, key, options.asideBytes ?? ASIDE_BYTES));
      return { place, ...(aside ? { aside } : {}) };
    }
    throw new Error(`This computer could not record which folder the history of ${bound.folder} belongs to, so it was not used`);
  }
  throw new Error(`The history of the folder ${bound.folder} was changed each time it was looked at, so it was not found`);
}
