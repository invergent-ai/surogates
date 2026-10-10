// A landing's writes into the user's folder (spec, Section 13, "Landing on the computer"): the
// file helper's `land` kind, in a helper started for a landing, which is given the thread's copy
// and a folder of the app's own to keep replaced files in. A turn's files come from the copy, and
// each goes over the real file only while that is the one the landing looked at.
//
// Nothing here trusts the copy or what named the files: the guest writes both, and the place the
// copy is in. A path is one inside the folder, the copy is a folder and no link in its stead, no
// link is on a file's way in the folder or in the copy, no protected name is written, the bytes
// that land are the ones the turn committed, by their git blob id, and a file is deleted only
// when the copy no longer holds it. A file's folder is entered once, through a handle on each
// folder above it, and everything the step does there goes through that handle: a link put on
// the way afterwards leads nowhere.
//
// A real file is never written in place and never renamed over. The one that is there is first
// moved aside, in its own directory, and looked at again there, where no save by its name can
// reach it any more: only if it is still the file the landing's look saw does the new file take
// its name, by a link, which fails where a name was taken meanwhile. The file moved aside is
// kept, in the app's data, until the landing is recorded (forget) or put back (unapply).
//
// These are the user's files, so a step can be cut at any point, by a kill too, and lose none.
// What a step is about to do is written down before it does it, each name it uses beside the
// real file with it: the file it stages, the one it moves aside. A step whose record does not
// say it ended is put back when a landing's helper next starts, before it looks at the folder
// or writes it: the real file takes its name again, or, where the user has put another there
// meanwhile, goes beside it under a name that says what it is.

import { createHash, randomUUID } from "node:crypto";
import {
  accessSync, type BigIntStats, chmodSync, closeSync, constants, copyFileSync, fchmodSync, fstatSync, fsyncSync, futimesSync, linkSync, lstatSync, mkdirSync,
  openSync, readdirSync, readFileSync, readSync, renameSync, rmdirSync, rmSync, unlinkSync, writeFileSync, writeSync,
} from "node:fs";
import { join } from "node:path";

import { Failure, fromNode, io, osError, sandboxError, valueError } from "./answers.js";
import { type Context, OWN_FILE, ownFile, revisionOf } from "./operations.js";
import { inFolderRefusal, protectedInFolder } from "./protect.js";

// The most files one look answers for: its answer is one message.
const MAX_LOOKED = 2_000;
const PIECE_BYTES = 1024 * 1024;
const ID = /^[0-9a-f]{40}$/;
// A saga's id, as a folder's name in the app's data.
const SAGA = /^[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}$/;
// A file's revision, as the look answers it (operations.ts, revisionOf).
const REVISION = /^[0-9]+:[0-9]+:[0-9]+:-?[0-9]+:-?[0-9]+$/;
// A folder held, not read: O_PATH, which Node does not name. One this user may pass through but not list is entered all the same.
const HOLD = 0o10000000 | constants.O_DIRECTORY | constants.O_NOFOLLOW;
// The largest file a landing writes: the helper copies it while every other operation on the folder waits.
const MAX_LAND_BYTES = 1024 * 1024 * 1024;
// The most the files a folder's landings replaced may take while they are kept. A file that would go past it is
// refused before it is touched, and lands once a landing before it was recorded or put back, which lets go of its own.
const MAX_KEPT_BYTES = 4 * 1024 * 1024 * 1024;
const LAND_EFBIG = new Failure({ type: "os", code: "EFBIG", message: `File too large to land in a local folder (over ${MAX_LAND_BYTES / 2 ** 30} GiB)` });
// What a file that could not take its own name back is called beside the one that holds it.
const BESIDE = "kept by Surogate";

// A forgotten saga's folder while it is removed: no saga's name starts with a dot.
const FORGOTTEN = ".forgotten-";

const BAD = "land takes revisions of paths, an apply or an unapply of a saga's step on a path, the forgetting of a saga, or a recovery";
const USED = "land's step was already used for another file of this saga";

// Which file a name holds, by what does not change when it is moved or linked: a change of its bytes moves its size or its time.
interface Identity {
  dev: string;
  ino: string;
  size: string;
  mtimeNs: string;
}

// What a step does, written down before it does it. Its own names beside the real file are each a
// `.surogate-<uuid>.tmp` there, and each is in the record before the file is made.
interface Step {
  path: string;
  was: Identity | null; // the real file it found, or none
  wrote: Identity | null; // the file it put there, or none for a deletion, and until it is staged
  mode: number | null; // the replaced file's
  made: string[]; // the folders it makes for its file, as paths in the folder, deepest first
  above: Array<[string, number]>; // a deletion's: the folders above its file, each with its mode, for one it empties
  temp: string | null; // its new file, until it is linked in
  aside: string | null; // the real file, while it is moved aside
  moved: boolean; // written down just before the real file leaves its name: until then it is at its name, whatever else is cut
  out: string | null; // a put-back's: the landing's file, moved out of the real one's way
  back: string | null; // a put-back's: the kept file's copy, on its way back from another filesystem
}

/** What a landing's helper put right at its start, of steps an earlier one was cut short in. */
export interface Recovered {
  restored: string[]; // the files that took their names again
  beside: Array<[string, string]>; // a file whose name the user gave another since: its path, and the path it is at now
  unread: Array<[string, number, string | null]>; // a record that cannot be read: its landing, its step, and the file it names if it still says; nothing is done for it
  lost: Array<[string, string]>; // a file whose folder is gone, or a link now: its path, and the name it has wherever that folder went
}

// As a revision spells them: unsigned.
const identityOf = (st: BigIntStats): Identity => ({
  dev: String(BigInt.asUintN(64, st.dev)), ino: String(BigInt.asUintN(64, st.ino)), size: String(st.size), mtimeNs: String(st.mtimeNs),
});
function same(st: BigIntStats | null, id: Identity | null): boolean {
  if (st === null || id === null) return st === null && id === null;
  const found = identityOf(st);
  return found.dev === id.dev && found.ino === id.ino && found.size === id.size && found.mtimeNs === id.mtimeNs;
}
// The same file, or its copy brought back from another filesystem: its size, and its time to the microsecond.
function alike(st: BigIntStats | null, id: Identity | null): boolean {
  if (st === null || id === null || same(st, id)) return same(st, id);
  return String(st.size) === id.size && st.mtimeNs / 1000n === BigInt(id.mtimeNs) / 1000n;
}
const plain = (st: BigIntStats): boolean => st.isFile() && st.nlink === 1n;
// A revision as an identity: its change time is left out, which a move changes.
function expectedIdentity(revision: string): Identity {
  const [dev = "", ino = "", size = "", mtimeNs = ""] = revision.split(":");
  return { dev, ino, size, mtimeNs };
}
// What the look answers for a real file: its revision, "absent", or "other" for anything a landing does not replace.
const tokenOf = (found: BigIntStats | null): string => (found === null ? "absent" : plain(found) ? revisionOf(found) : "other");
const settled = (step: Step): boolean => step.temp === null && step.aside === null && step.out === null && step.back === null;

function look(path: string): BigIntStats | null {
  try {
    return lstatSync(path, { bigint: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw fromNode(error, path);
  }
}

// The refusal of a file where the folder gives none a second name: a landing takes a name only by a link, which
// fails where another file took it meanwhile, and gives a file its own name back the same way.
function unlinkable(path: string, error: unknown): unknown {
  const { code } = error as NodeJS.ErrnoException;
  if (typeof code !== "string" || code === "ENOENT") return code === "ENOENT" ? conflict(path) : error;
  return new Failure({ type: "os", code, message: `${path} was not written: this folder cannot give a file a second name, which a landing needs to put back what it replaces` });
}

function conflict(path: string): Failure {
  return new Failure({ type: "conflict", message: `${path} changed in the folder while this landing ran, so it was not replaced` });
}

// A path as git names a file: its parts, none empty, "." or "..", and no NUL.
function partsOf(path: unknown): string[] {
  if (typeof path !== "string" || path === "" || path.includes("\0") || path.startsWith("/")) throw valueError(BAD);
  const parts = path.split("/");
  if (parts.some((part) => part === "" || part === "." || part === "..")) throw valueError(BAD);
  return parts;
}

/** Whether a landing may write the file git names *path*: a path inside the folder, and no name whose change runs code on this computer. */
export function landable(path: string): boolean {
  try {
    return !protectedInFolder("/folder", join("/folder", ...partsOf(path)));
  } catch {
    return false;
  }
}

// A folder held open. What is in it is reached through the handle, never by the folder's path
// again, so nothing put on that path since is followed (operations.ts, walk).
class Held {
  constructor(readonly fd: number) {}

  at(name: string): string {
    return `/proc/self/fd/${this.fd}/${name}`;
  }

  close(): void {
    closeSync(this.fd);
  }
}

// The folder *name* in *dir*, held: null where there is none, and refused where what is there is no folder, a link least of all.
function into(dir: Held, name: string, path: string): Held | null {
  try {
    return new Held(openSync(dir.at(name), HOLD));
  } catch (error) {
    const { code } = error as NodeJS.ErrnoException;
    if (code === "ENOENT") return null;
    if (code === "ENOTDIR" || code === "ELOOP") throw sandboxError(`Not a path in this folder: '${path}'`);
    throw fromNode(error, path);
  }
}

// Where the folders *names* lead from *from*, which is let go: the last one, held, or null with how many of them
// are there; and each one's mode. With *make*, the missing ones are made, *made* told the place in *names* of each.
function enter(from: Held, names: string[], path: string, make = false, made?: (index: number, dir: Held) => void): { dir: Held | null; depth: number; modes: number[] } {
  let dir = from;
  const modes: number[] = [];
  try {
    for (const [depth, name] of names.entries()) {
      let next = into(dir, name, path);
      if (next === null) {
        if (!make) {
          dir.close();
          return { dir: null, depth, modes };
        }
        try {
          mkdirSync(dir.at(name));
        } catch (error) {
          // Made by another meanwhile: it is entered as any other.
          if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw fromNode(error, path);
        }
        next = into(dir, name, path);
        if (next === null) throw osError("ENOENT", path);
        made?.(depth, next);
      }
      dir.close();
      dir = next;
      modes.push(fstatSync(dir.fd).mode & 0o7777);
    }
    return { dir, depth: names.length, modes };
  } catch (error) {
    dir.close();
    throw error;
  }
}

function syncDir(dir: string): void {
  let fd: number | undefined;
  try {
    fd = openSync(dir, constants.O_RDONLY | constants.O_DIRECTORY);
    fsyncSync(fd);
  } catch {
    // A filesystem that syncs no folder: the files in it were synced themselves.
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

// Gone, or never there. Any other failure is raised: a file that could not be removed is still in the folder, and
// whoever asked must not go on as if it were not.
function remove(path: string | null): void {
  if (path === null) return;
  try {
    unlinkSync(path);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw fromNode(error, path);
  }
}

// Whether this user may give *found*, the file at *at*, a second name: any file of their own, and another's only
// as the kernel allows it (fs.protected_hardlinks): one they may read and write that runs as nobody else.
function relinkable(found: BigIntStats, at: string): boolean {
  if (Number(found.uid) === process.getuid?.()) return true;
  if ((found.mode & 0o4000n) !== 0n || (found.mode & 0o2010n) === 0o2010n) return false;
  try {
    accessSync(at, constants.R_OK | constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

// *fd*, a copy made across filesystems, given the times of the file at *from*, to the microsecond: a
// move keeps a file's times, and a put-back's file is the one the user saved then, not one made now.
// Node sets a time from seconds as a float, which at today's dates is exact to a quarter of a microsecond and is cut
// down to one: half a microsecond past the one meant, so that the cut never lands on the one before it.
function dated(fd: number, from: string): void {
  const { atimeNs, mtimeNs } = lstatSync(from, { bigint: true });
  const seconds = (ns: bigint) => (Number(ns / 1000n) + 0.5) / 1e6;
  futimesSync(fd, seconds(atimeNs), seconds(mtimeNs));
}

// *name* with what it is said after its stem, short enough to be a name: "Report (kept by Surogate).docx".
function besideName(name: string, count: number): string {
  const dot = name.lastIndexOf(".");
  let stem = dot > 0 ? name.slice(0, dot) : name;
  const rest = ` (${BESIDE}${count > 1 ? ` ${count}` : ""})${dot > 0 ? name.slice(dot) : ""}`;
  while (stem.length > 1 && Buffer.byteLength(stem + rest) > 255) stem = [...stem].slice(0, -1).join("");
  return stem + rest;
}

// The helper's own *own* in *dir*, which holds what was at *name*, takes that name again. Never over what holds it
// now: then it takes the first free name that says what it is, which is answered. Null when it is back at its own.
function restore(dir: Held, own: string, name: string): string | null {
  const from = dir.at(own);
  const held = look(from);
  if (held === null) return null;
  for (let count = 0; count < 100; count += 1) {
    const to = count === 0 ? name : besideName(name, count);
    const there = look(dir.at(to));
    if (there !== null) {
      // Its own second name, from a restore cut short after its link.
      if (there.dev === held.dev && there.ino === held.ino) {
        remove(from);
        return count === 0 ? null : to;
      }
      continue;
    }
    try {
      // A link fails where the name was taken meanwhile. A folder, which a save can leave where the file was, has no second name to give.
      if (held.isDirectory()) renameSync(from, dir.at(to));
      else {
        linkSync(from, dir.at(to));
        unlinkSync(from);
      }
      return count === 0 ? null : to;
    } catch (error) {
      if (!["EEXIST", "ENOTEMPTY", "ENOTDIR", "EISDIR"].includes((error as NodeJS.ErrnoException).code ?? "")) throw fromNode(error, name);
    }
  }
  throw osError("EEXIST", name);
}

// The copy's file *name* in its folder *theirs*, opened to read: only a plain file of the copy's, and no link at its name.
function sourceOf(theirs: Held | null, name: string, path: string): number {
  if (theirs === null) throw osError("ENOENT", path);
  let fd: number;
  try {
    fd = openSync(theirs.at(name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  } catch (error) {
    // A link at its name is no file of the copy's.
    if ((error as NodeJS.ErrnoException).code === "ELOOP") throw sandboxError(`Not a path in this folder: '${path}'`);
    throw fromNode(error, path);
  }
  const opened = fstatSync(fd);
  if (!opened.isFile() || opened.size > MAX_LAND_BYTES) {
    closeSync(fd);
    // From its size, before a byte of it is read or anything is made for it.
    throw opened.isFile() ? LAND_EFBIG : sandboxError(`Not a path in this folder: '${path}'`);
  }
  return fd;
}

// The copy's file, open at *from*, written at *temp*, a name of the landing's own beside the real file: only where
// its bytes are the blob *after*. Which file it wrote.
function stage(from: number, temp: string, path: string, after: string, mode: number | null): Identity {
  const { size } = fstatSync(from);
  const to = io(path, () => openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o666));
  try {
    const hash = createHash("sha1").update(`blob ${size}\0`);
    const piece = Buffer.allocUnsafe(PIECE_BYTES);
    let read = 0;
    for (;;) {
      const count = io(path, () => readSync(from, piece, 0, piece.length, null));
      // A file that grows as it is read is no longer the turn's: no more of it is copied than the turn's was.
      if (count === 0 || read > size) break;
      read += count;
      hash.update(piece.subarray(0, count));
      for (let written = 0; written < count;) written += io(path, () => writeSync(to, piece, written, count - written));
    }
    if (read !== size || hash.digest("hex") !== after) {
      throw new Failure({ type: "stale", message: `${path} changed in the thread's copy after its turn was committed, so it was not landed` });
    }
    if (mode !== null) fchmodSync(to, mode);
    fsyncSync(to);
    return identityOf(fstatSync(to, { bigint: true }));
  } finally {
    closeSync(to);
  }
}

// A record as this module writes one, or null: nothing else in it is acted on.
function stepOf(value: unknown): Step | null {
  if (typeof value !== "object" || value === null) return null;
  const { path, was, wrote, mode, made, above, temp, aside, moved, out, back } = value as Record<string, unknown>;
  const identity = (id: unknown): id is Identity | null =>
    id === null || (typeof id === "object" && ["dev", "ino", "size", "mtimeNs"].every((key) => /^-?[0-9]+$/.test(String((id as Record<string, unknown>)[key]))));
  const own = (name: unknown): name is string | null => name === null || (typeof name === "string" && OWN_FILE.test(name));
  const inside = (dir: unknown): dir is string => {
    try {
      return partsOf(dir).length > 0;
    } catch {
      return false;
    }
  };
  const whole = (count: unknown): count is number => typeof count === "number" && Number.isInteger(count) && count >= 0 && count <= 0o7777;
  if (
    !inside(path) || !identity(was) || !identity(wrote) || !(mode === null || whole(mode)) || !Array.isArray(made) || !made.every(inside)
    || !Array.isArray(above) || !above.every((one) => Array.isArray(one) && one.length === 2 && inside(one[0]) && whole(one[1]))
    || !own(temp) || !own(aside) || !own(out) || !own(back) || typeof moved !== "boolean"
  ) {
    return null;
  }
  return { path, was, wrote, mode, made, above: above as Array<[string, number]>, temp, aside, moved, out, back };
}

// How many bytes each folder's landings keep, by where they keep them: counted once, kept up as a file is kept, and
// counted again after anything was dropped. One helper holds a folder, so nothing else keeps or drops there.
const keeping = new Map<string, number>();

function keptBytes(store: string): number {
  let bytes = keeping.get(store);
  if (bytes !== undefined) return bytes;
  bytes = 0;
  const list = (dir: string): string[] => {
    try {
      return readdirSync(dir);
    } catch {
      return [];
    }
  };
  for (const saga of list(store).filter((name) => SAGA.test(name))) {
    for (const name of list(join(store, saga))) {
      if (/^[0-9]+$/.test(name)) bytes += Number(look(join(store, saga, name))?.size ?? 0n);
    }
  }
  keeping.set(store, bytes);
  return bytes;
}

// A record that is there and cannot be acted on.
class Unreadable extends Failure {
  constructor(readonly saga: string, readonly step: number, readonly path: string | null) {
    super({ type: "os", code: "EIO", message: `The record of step ${step} of landing ${saga} cannot be read, so nothing is done over it` });
  }
}

class Landing {
  private readonly folder: string;
  private readonly copy: string;
  private readonly store: string;
  private readonly kept: string;
  private readonly key: string;

  constructor(context: Context, private readonly saga: string) {
    this.key = keyOf(context);
    this.folder = context.folder;
    this.copy = context.landing!.copy;
    this.store = context.landing!.kept;
    this.kept = join(this.store, saga);
  }

  private record(step: number): string {
    return join(this.kept, `${step}.json`);
  }

  private bytes(step: number): string {
    return join(this.kept, String(step));
  }

  // Synced before the step moves the real file: a record that only names a file about to be staged need not be.
  private write(step: number, done: Step, sync = true): void {
    io(done.path, () => {
      mkdirSync(this.kept, { recursive: true, mode: 0o700 });
      const temp = `${this.record(step)}.new`;
      const fd = openSync(temp, "w", 0o600);
      try {
        writeFileSync(fd, JSON.stringify(done));
        if (sync) fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      renameSync(temp, this.record(step));
    });
    if (sync) syncDir(this.kept);
  }

  // The step's record, or null where it has none. One that is there and is not a record as this module writes it,
  // or names a file no landing may write, is a failure raised to whoever asked: it may be all that names a file
  // moved aside, so nothing is done over it, and nothing by it.
  private read(step: number): Step | null {
    let text: string;
    try {
      text = readFileSync(this.record(step), "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw new Unreadable(this.saga, step, null);
    }
    let value: unknown = null;
    try {
      value = JSON.parse(text);
    } catch {
      // Not even JSON.
    }
    const did = stepOf(value);
    if (did !== null && !protectedInFolder(this.folder, join(this.folder, ...did.path.split("/")))) return did;
    const named = (value as { path?: unknown } | null)?.path;
    throw new Unreadable(this.saga, step, typeof named === "string" && landable(named) ? named : null);
  }

  // What a step kept goes: its record, and the file it replaced; and the saga's folder with its last step's.
  private drop(step: number): void {
    // The record last: cut between the two, it still says what was under way.
    remove(`${this.bytes(step)}.part`);
    remove(this.bytes(step));
    remove(this.record(step));
    this.rmdir(this.kept);
    keeping.delete(this.store);
  }

  private rmdir(dir: string): boolean {
    try {
      rmdirSync(dir);
      return true;
    } catch {
      return false;
    }
  }

  // The refusal of a file this landing wrote already, under another of its names: a folder that tells names apart
  // less than git does, by their case or by how their letters are composed, holds one file where the copy holds two.
  private twice(found: BigIntStats | null, path: string): Failure | null {
    let names: string[] = [];
    try {
      names = readdirSync(this.kept);
    } catch {
      // It has written nothing yet.
    }
    for (const name of found === null ? [] : names) {
      const step = /^(0|[1-9][0-9]*)\.json$/.exec(name)?.[1];
      let did: Step | null = null;
      try {
        did = step === undefined ? null : this.read(Number(step));
      } catch {
        // A record that cannot be read names no file this one could be.
      }
      if (did !== null && did.path !== path && did.wrote !== null && same(found, did.wrote)) {
        return new Failure({
          type: "os", code: "EEXIST",
          message: `${path} and ${did.path} are one file in this folder, which tells names apart less than the thread's copy does, so it was not written twice`,
        });
      }
    }
    return null;
  }

  // The folder itself, held: the app gave it resolved, and a link in its stead since is no folder of the chat's.
  private root(path: string): Held {
    return new Held(io(path, () => openSync(this.folder, HOLD)));
  }

  // The folders *dirs*, paths in the folder with the deepest first, go while they are empty, and no further up than one that is not.
  private empty(dirs: string[]): void {
    for (const dir of dirs) {
      const parts = dir.split("/");
      let above: Held | null = null;
      try {
        above = enter(this.root(dir), parts.slice(0, -1), dir).dir;
        if (above === null || !this.rmdir(above.at(parts.at(-1)!))) return;
      } catch {
        return;
      } finally {
        above?.close();
      }
    }
  }

  // The thread's copy as far as the folders *names*: only a folder of the app's own, never a link in its stead.
  private theirs(names: string[], path: string): Held | null {
    let copy: Held;
    try {
      // The guest writes the place the copy is in: a link in the copy's stead would name any folder it likes, and
      // a copy that is gone holds nothing, which is no thread's deletion of every file.
      copy = new Held(openSync(this.copy, HOLD));
    } catch {
      throw sandboxError("This thread's copy is not a folder of the app's own");
    }
    return enter(copy, names, path).dir;
  }

  apply(step: number, path: string, before: string | null, after: string | null, expected: string): unknown {
    const parts = partsOf(path);
    const name = parts.at(-1)!;
    const folders = parts.slice(0, -1);
    if (protectedInFolder(this.folder, join(this.folder, ...parts))) throw sandboxError(inFolderRefusal(path));
    const held: Held[] = [];
    let source: number | null = null;
    try {
      const theirs = this.theirs(folders, path);
      if (theirs !== null) held.push(theirs);
      const way = enter(this.root(path), folders, path);
      if (way.dir !== null) held.push(way.dir);
      const found = way.dir === null ? null : look(way.dir.at(name));
      const done = { path, before, after };
      // A step that ran already, as after an answer that was lost: the real file is what it wrote. Its record is
      // never written over: the file it kept is the user's.
      const earlier = this.read(step);
      if (earlier !== null) {
        if (earlier.path !== path) throw valueError(USED);
        if (settled(earlier) && same(found, earlier.wrote)) return { ...done, made: earlier.made };
        throw conflict(path);
      }
      // The look, again, before anything is made: a file saved since is no file of this landing's.
      if (tokenOf(found) !== expected) throw this.twice(found, path) ?? conflict(path);
      if (after === null && found === null) return { ...done, made: [] };
      // A landing deletes only what its thread deleted: a name its copy still holds is not the turn's
      // deletion, whoever asks for it.
      if (after === null && theirs !== null && look(theirs.at(name)) !== null) {
        throw new Failure({ type: "stale", message: `${path} is still in the thread's copy, so it was not deleted from the folder` });
      }
      if (after !== null) source = sourceOf(theirs, name, path);
      // A file this user could move aside and then not give its name back is not moved.
      if (found !== null && !relinkable(found, way.dir!.at(name))) throw unlinkable(path, Object.assign(new Error("EPERM"), { code: "EPERM" }));
      if (found !== null && keptBytes(this.store) + Number(found.size) > MAX_KEPT_BYTES) {
        throw new Failure({
          type: "os", code: "EDQUOT",
          message: `${path} was not replaced: more than ${MAX_KEPT_BYTES / 2 ** 30} GiB would be kept of the files this folder's landings replaced`,
        });
      }
      const pathTo = (count: number) => folders.slice(0, count).join("/");
      const mode = found === null ? null : Number(found.mode & 0o7777n);
      let did: Step = {
        path, was: found === null ? null : identityOf(found), wrote: null, mode,
        made: folders.slice(way.depth).map((_, index) => pathTo(way.depth + index + 1)).reverse(),
        // A deletion takes the folders it empties with it: each one's mode, for its put-back to make it again as it was.
        above: after === null ? way.modes.map((one, index) => [pathTo(index + 1), one]) : [],
        temp: after === null ? null : ownFile(), aside: found === null ? null : ownFile(), moved: false,
        // The two names its look for a second name uses: a deletion stages no file of its own to give one to.
        out: found === null ? null : ownFile(), back: found === null || after !== null ? null : ownFile(),
      };
      this.write(step, did, source === null);
      let dir = way.dir;
      try {
        if (dir === null) {
          dir = enter(this.root(path), folders, path, true).dir!;
          held.push(dir);
        }
        if (source !== null) {
          did = { ...did, wrote: stage(source, dir.at(did.temp!), path, after!, mode) };
          this.write(step, did);
        }
        this.still(dir, folders, path);
        if (did.aside !== null) {
          const [real, aside] = [dir.at(name), dir.at(did.aside)];
          // Before the real file leaves its name: the folder gives a file a second name, which is how the real one
          // takes its own back. Seen with a file of the landing's own, so the user's never has two names.
          const [first, second] = [dir.at(did.temp ?? did.back!), dir.at(did.out!)];
          try {
            if (did.temp === null) closeSync(openSync(first, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600));
            linkSync(first, second);
          } catch (error) {
            throw unlinkable(path, error);
          }
          remove(second);
          if (did.temp === null) remove(first);
          // Not synced: a kill loses nothing written, and after a power cut a step that may have moved its file is taken to have.
          did = { ...did, moved: true };
          this.write(step, did, false);
          try {
            renameSync(real, aside);
          } catch (error) {
            did = { ...did, moved: false };
            throw fromNode(error, path);
          }
          // Moved aside, no save by its name reaches it: it is the file the look saw, or it goes back.
          const moved = look(aside);
          if (moved === null || !plain(moved) || !same(moved, expectedIdentity(expected))) throw conflict(path);
        }
        if (did.temp !== null) {
          try {
            linkSync(dir.at(did.temp), dir.at(name));
          } catch (error) {
            // A file was made at its name meanwhile: that one stays.
            throw (error as NodeJS.ErrnoException).code === "EEXIST" ? conflict(path) : unlinkable(path, error);
          }
          remove(dir.at(did.temp));
        }
        if (did.aside !== null) this.keep(dir.at(did.aside), step);
        this.write(step, { ...did, temp: null, aside: null, moved: false, out: null, back: null });
        if (after === null) this.empty(folders.map((_, index) => pathTo(folders.length - index)));
        syncDir(dir.at("."));
        return { ...done, made: did.made };
      } catch (error) {
        throw this.undone(step, did, error, dir);
      }
    } finally {
      if (source !== null) closeSync(source);
      for (const dir of held) dir.close();
    }
  }

  // The folder the step works in is still the one at its path: one moved away while the copy's file was staged is no longer where the landing writes.
  private still(dir: Held, folders: string[], path: string): void {
    const now = enter(this.root(path), folders, path).dir;
    try {
      const [held, there] = [fstatSync(dir.fd, { bigint: true }), now === null ? null : fstatSync(now.fd, { bigint: true })];
      if (there === null || there.dev !== held.dev || there.ino !== held.ino) throw conflict(path);
    } finally {
      now?.close();
    }
  }

  // *from*, the real file moved aside, kept in the app's data: moved, or copied where that is another filesystem.
  private keep(from: string, step: number): void {
    const to = this.bytes(step);
    try {
      renameSync(from, to);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EXDEV") throw fromNode(error, from);
      // Under another name until it is whole: a kept file that has its name is the user's file, all of it.
      const part = `${to}.part`;
      copyFileSync(from, part);
      // Read-only: a file the user may only read is copied with that mode, and is theirs to date and sync all the same.
      const fd = openSync(part, constants.O_RDONLY | constants.O_NOFOLLOW);
      try {
        dated(fd, from);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      renameSync(part, to);
      syncDir(this.kept);
      unlinkSync(from);
    }
    // The user's file, in the app's data: this user's alone to open there, whatever its mode was in the folder.
    chmodSync(to, 0o600);
    const counted = keeping.get(this.store);
    if (counted !== undefined) keeping.set(this.store, counted + Number(look(to)?.size ?? 0n));
  }

  // The file *held*, the one a step replaced, takes *name* in *dir* again, only where nothing holds it: a link,
  // or a copy linked in where the kept file is on another filesystem. False where the name was taken meanwhile.
  private place(step: number, did: Step, dir: Held, held: string, name: string): boolean {
    // What was moved aside and found to be no file of the landing's can be a folder, made where the file was since
    // the look: it has no second name to give, so it is moved back, where nothing holds its name.
    if (look(held)?.isDirectory()) {
      if (look(dir.at(name)) !== null) return false;
      io(did.path, () => renameSync(held, dir.at(name)));
      return true;
    }
    const kept = held === this.bytes(step);
    try {
      // A kept file takes its own mode again before it takes its name.
      if (kept && did.mode !== null) chmodSync(held, did.mode);
      linkSync(held, dir.at(name));
      return true;
    } catch (error) {
      if (kept) chmodSync(held, 0o600);
      const { code } = error as NodeJS.ErrnoException;
      if (code === "EEXIST") return false;
      if (code !== "EXDEV") throw fromNode(error, did.path);
    }
    const back = did.back ?? ownFile();
    if (did.back === null) this.write(step, { ...did, back });
    const staged = dir.at(back);
    // A copy an earlier put-back was cut short in.
    remove(staged);
    try {
      copyFileSync(held, staged, constants.COPYFILE_EXCL);
      const fd = openSync(staged, constants.O_RDONLY | constants.O_NOFOLLOW);
      try {
        if (did.mode !== null) fchmodSync(fd, did.mode);
        dated(fd, held);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      linkSync(staged, dir.at(name));
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
      throw fromNode(error, did.path);
    } finally {
      remove(staged);
    }
  }

  // A step put back, from whatever its record says it reached: the real file at its name again, and nothing of the
  // step left. "restored" where the real file had left its name and took it again, "back" where it never had.
  // "changed" where the name holds a file that is neither the step's nor the one it found: that file stays, and so
  // does what the step kept.
  private putBack(step: number, record: Step, held: Held | null = null): "back" | "restored" | "changed" {
    let did = record;
    const parts = did.path.split("/");
    const name = parts.at(-1)!;
    const folders = parts.slice(0, -1);
    // The file's folder as the step holds it, where it still does: what it left there is found whatever its path leads to now.
    let dir = held ?? enter(this.root(did.path), folders, did.path).dir;
    try {
      const own = (one: string | null): string | null => (one !== null && dir !== null && look(dir.at(one)) !== null ? dir.at(one) : null);
      // The file the step replaced: still moved aside beside the real one, or kept.
      const replaced = own(did.aside) ?? (look(this.bytes(step)) !== null ? this.bytes(step) : null);
      const now = dir === null ? null : look(dir.at(name));
      let outcome: "back" | "restored" = "back";
      // Written down as under way before anything of it is done, with the name it moves the landing's file to: cut
      // anywhere from here, the next start ends it.
      if (did.out === null) {
        did = { ...did, out: ownFile() };
        this.write(step, did);
      }
      if (dir !== null && did.wrote !== null && same(now, did.wrote)) {
        if (did.was !== null && replaced === null) return "changed";
        // Its own file is there: moved out as the real one was, and looked at again before anything takes its place.
        const [real, out] = [dir.at(name), dir.at(did.out!)];
        io(did.path, () => renameSync(real, out));
        if (!same(look(out), did.wrote)) {
          restore(dir, did.out!, name);
          return "changed";
        }
        if (did.was !== null) {
          if (!this.place(step, did, dir, replaced!, name)) {
            // The name was taken while the landing's file left it: that file stays, and the landing's goes.
            remove(out);
            return "changed";
          }
          outcome = "restored";
        }
      } else if (now === null && did.was !== null) {
        // A deletion, or a write cut short with the real file moved aside: the replaced file takes its name again.
        if (replaced === null) return "changed";
        if (dir === null) {
          const modes = new Map(did.above);
          dir = enter(this.root(did.path), folders, did.path, true, (index, made) => {
            const mode = modes.get(folders.slice(0, index + 1).join("/"));
            if (mode !== undefined) chmodSync(`/proc/self/fd/${made.fd}`, mode);
          }).dir!;
        }
        if (!this.place(step, did, dir, replaced, name)) return "changed";
        outcome = "restored";
      } else if (!alike(now, did.was)) {
        // Neither the step's file nor the one it found: someone else's change.
        return "changed";
      }
      this.release(step, did, dir, false);
      if (did.was === null) this.empty(did.made);
      if (dir !== null) syncDir(dir.at("."));
      return outcome;
    } finally {
      if (dir !== held) dir?.close();
    }
  }

  // The one way a record lets go of its step: dropped, or written down as ended with what it kept. Never while a
  // file the step moved aside is still under the step's name for it, nor any other file of the step's own: one that
  // cannot be taken away is a failure raised here, and the record stays to name it.
  private release(step: number, did: Step, dir: Held | null, ended: boolean): void {
    if (dir !== null) {
      for (const one of [did.temp, did.out, did.back]) if (one !== null) remove(dir.at(one));
      const aside = did.aside === null ? null : look(dir.at(did.aside));
      // Taken away only as the second name of a file that has its own again.
      if (aside !== null && (aside.isDirectory() || aside.nlink < 2n)) throw osError("EEXIST", did.path, "A file moved aside has not taken its name back");
      if (aside !== null) remove(dir.at(did.aside!));
    }
    if (ended) this.write(step, { ...did, temp: null, aside: null, moved: false, out: null, back: null });
    else this.drop(step);
  }

  // A step that did not end, put back: by a failure of its own, or found by a helper's start after a kill.
  private revert(step: number, did: Step, report: Recovered, held: Held | null = null): void {
    let outcome: "back" | "restored" | "changed" = "changed";
    try {
      outcome = this.putBack(step, did, held);
    } catch (error) {
      // A link on its way now: nothing is followed to find what was moved aside there.
      if (!(error instanceof Failure && error.refusal.type === "sandbox")) throw error;
    }
    if (outcome === "restored") report.restored.push(did.path);
    if (outcome === "changed") this.end(step, report, held);
  }

  // A step that cannot be put back, ended: the name holds a file that is neither the step's nor the one it found,
  // which stays. The file the step moved aside goes beside it, and is said; what the step kept stays kept, as a
  // whole step's does, for whoever settles the landing.
  private end(step: number, report: Recovered, held: Held | null = null): void {
    const did = this.read(step);
    if (did === null) return;
    const parts = did.path.split("/");
    let dir = held;
    try {
      dir ??= enter(this.root(did.path), parts.slice(0, -1), did.path).dir;
    } catch (error) {
      if (!(error instanceof Failure && error.refusal.type === "sandbox")) throw error;
    }
    try {
      const kept = look(this.bytes(step)) !== null;
      if (dir !== null && did.aside !== null && look(dir.at(did.aside)) !== null) {
        const beside = restore(dir, did.aside, parts.at(-1)!);
        if (beside === null) report.restored.push(did.path);
        else report.beside.push([did.path, [...parts.slice(0, -1), beside].join("/")]);
      } else if (did.moved && !kept && !(dir !== null && alike(look(dir.at(parts.at(-1)!)), did.was))) {
        // The file left its name, and is neither where the step moved it nor kept whole: it went with its folder,
        // wherever that is now. The record is all that names it, so it stays, and says so at every start.
        report.lost.push([did.path, did.aside!]);
        return;
      }
      this.release(step, did, dir, kept);
      if (!kept && did.was === null) this.empty(did.made);
    } finally {
      if (dir !== held) dir?.close();
    }
  }

  // A step that failed leaves nothing of itself. The failure it is answered with, which says so where the real
  // file could not take its own name back.
  private undone(step: number, did: Step, error: unknown, held: Held | null): unknown {
    const report: Recovered = { restored: [], beside: [], lost: [], unread: [] };
    try {
      this.revert(step, did, report, held);
    } catch {
      // Its record stays, and names what it left. This helper's look for steps cut short is no longer done: the
      // next thing it is asked puts the step back first, or is refused as this was.
      recovered.delete(this.key);
      return error;
    }
    const beside = report.beside[0]?.[1];
    if (beside === undefined || !(error instanceof Failure)) return error;
    return new Failure({ ...error.refusal, message: `${error.refusal.message}; the file that was there is beside it as '${beside}'` });
  }

  unapply(step: number, path: string): unknown {
    const parts = partsOf(path);
    const did = this.read(step);
    if (did === null) return { path, put_back: false };
    if (did.path !== path) throw valueError(BAD);
    if (protectedInFolder(this.folder, join(this.folder, ...parts))) throw sandboxError(inFolderRefusal(path));
    if (this.putBack(step, did) === "changed") {
      // The step ends as one cut short does, and what it kept stays kept.
      const report: Recovered = { restored: [], beside: [], lost: [], unread: [] };
      this.end(step, report);
      const beside = report.beside[0]?.[1];
      throw new Failure({
        type: "conflict",
        message: `${path} changed after the landing wrote it, so it was not put back${beside === undefined ? "" : `; the file that was there before is beside it as '${beside}'`}`,
      });
    }
    return { path, put_back: true };
  }

  // Out of the sagas' names in one move, then removed: cut short, what is left is no landing's, and the next start
  // removes it. Refused while a step of the saga has not ended: its record is all that names a file moved aside.
  forget(): unknown {
    for (const step of this.steps()) {
      const did = this.read(step);
      if (did !== null && !settled(did)) {
        throw new Failure({ type: "conflict", message: `${did.path} was left by a step cut short and is not put back yet, so its landing was not forgotten` });
      }
    }
    const gone = join(this.store, `${FORGOTTEN}${randomUUID()}`);
    try {
      renameSync(this.kept, gone);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
      throw fromNode(error, this.kept);
    }
    rmSync(gone, { recursive: true, force: true });
    keeping.delete(this.store);
    return {};
  }

  // The steps the saga has a record of.
  private steps(): number[] {
    let names: string[] = [];
    try {
      names = readdirSync(this.kept);
    } catch {
      // It has written nothing yet.
    }
    return names.flatMap((name) => /^(0|[1-9][0-9]*)\.json$/.exec(name)?.[1] ?? []).map(Number);
  }

  // Every step of the saga whose record does not say it ended is put back, and a record's own half-written file
  // goes. A record that cannot be read is said and left. One step that cannot be put back does not keep the others
  // waiting: its failure is raised after them.
  mend(report: Recovered): void {
    const failures: unknown[] = [];
    for (const step of this.steps()) {
      try {
        const did = this.read(step);
        if (did !== null && !settled(did)) this.revert(step, did, report);
      } catch (error) {
        if (error instanceof Unreadable) report.unread.push([error.saga, error.step, error.path]);
        else failures.push(error);
      }
    }
    try {
      for (const name of readdirSync(this.kept)) if (name.endsWith(".json.new")) remove(join(this.kept, name));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") failures.push(error);
    }
    this.rmdir(this.kept);
    if (failures.length > 0) throw failures[0];
  }
}

// What each landing's helper found at its start, by the folder it lands in and where it keeps: one helper holds a
// folder, so a step is only ever cut short by the helper's own end, and one look for such steps lasts a helper's life.
const recovered = new Map<string, Recovered>();
const keyOf = (context: Context): string => `${context.folder}\0${context.landing!.kept}`;

/** What an earlier helper's landings left cut short in this folder, put back; once for a helper, and before it looks at the folder or writes it. */
export function recover(context: Context): Recovered {
  const { kept } = context.landing!;
  const key = keyOf(context);
  const known = recovered.get(key);
  if (known) return known;
  const report: Recovered = { restored: [], beside: [], lost: [], unread: [] };
  let sagas: string[] = [];
  try {
    sagas = readdirSync(kept);
  } catch {
    // Nothing was ever kept.
  }
  const failures: unknown[] = [];
  for (const saga of sagas) {
    if (saga.startsWith(FORGOTTEN)) rmSync(join(kept, saga), { recursive: true, force: true });
    if (!SAGA.test(saga)) continue;
    try {
      new Landing(context, saga).mend(report);
    } catch (error) {
      failures.push(error);
    }
  }
  // Not taken for done: nothing lands in a folder that still holds a step cut short, and the next ask tries again.
  if (failures.length > 0) throw failures[0];
  report.unread.sort(([saga, step], [other, next]) => (saga === other ? step - next : saga < other ? -1 : 1));
  recovered.set(key, report);
  return report;
}

/** The file helper's `land` kind: a landing's look at the real files, its applies and put-backs, and its forgetting. */
export function land(args: Record<string, unknown>, context: Context): unknown {
  // No helper but a landing's has a copy to land from.
  if (!context.landing) throw new Failure({ type: "unsupported", message: "This computer cannot do 'land' yet" });
  const { action, saga, step, path, before, after, expected, paths } = args;
  const report = recover(context);
  if (action === "recover") return report;
  if (action === "revisions") {
    if (!Array.isArray(paths) || paths.length > MAX_LOOKED) throw valueError(BAD);
    const revisions = paths.map((one): [string, string] => [one as string, revision(context.folder, partsOf(one), one as string)]);
    // Two names of the look that are one file, in a folder that tells names apart less than git does: neither is
    // replaced. A revision names one file, so two names with one revision are that.
    const named = new Map<string, Set<string>>();
    for (const [one, token] of revisions) named.set(token, (named.get(token) ?? new Set()).add(one));
    return { revisions: revisions.map(([one, token]) => [one, REVISION.test(token) && named.get(token)!.size > 1 ? "other" : token]) };
  }
  if (typeof saga !== "string" || !SAGA.test(saga)) throw valueError(BAD);
  const landing = new Landing(context, saga);
  if (action === "forget") return landing.forget();
  if (typeof step !== "number" || !Number.isSafeInteger(step) || step < 0 || typeof path !== "string") throw valueError(BAD);
  if (action === "unapply") return landing.unapply(step, path);
  const id = (value: unknown): value is string | null => value === null || (typeof value === "string" && ID.test(value));
  const looked = typeof expected === "string" && (expected === "absent" || REVISION.test(expected));
  if (action !== "apply" || !id(before) || !id(after) || (before === null && after === null) || !looked) throw valueError(BAD);
  return landing.apply(step, path, before, after, expected as string);
}

// What the look answers for the real file at *parts*: nothing through a link, which is "other" as any file a landing does not replace is.
function revision(folder: string, parts: string[], path: string): string {
  let dir: Held | null = null;
  try {
    dir = enter(new Held(openSync(folder, HOLD)), parts.slice(0, -1), path).dir;
    return dir === null ? "absent" : tokenOf(look(dir.at(parts.at(-1)!)));
  } catch {
    return "other";
  } finally {
    dir?.close();
  }
}
