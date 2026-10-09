// A landing's writes into the user's folder (spec, Section 13, "Landing on the computer"): the
// file helper's `land` kind, in a helper started for a landing, which is given the thread's copy
// and a folder of the app's own to keep replaced files in. A turn's files come from the copy, and
// each goes over the real file only while that is the one the landing looked at.
//
// Nothing here trusts the copy or what named the files: the guest writes both, and the place the
// copy is in. A path is one inside the folder, the copy is a folder and no link in its stead, no
// link is on a file's way in the folder or in the copy, no protected name is written, the bytes
// that land are the ones the turn committed, by their git blob id, and a file is deleted only
// when the copy no longer holds it.
//
// A real file is never written in place and never renamed over. The one that is there is first
// moved aside, in its own directory, and looked at again there, where no save by its name can
// reach it any more: only if it is still the file the landing's look saw does the new file take
// its name, by a link, which fails where a name was taken meanwhile. The file moved aside is
// kept, in the app's data, until the landing is recorded (forget) or put back (unapply). What
// each step did is written down before it changes anything, so a put-back needs no answer the
// apply never gave.

import { createHash, randomUUID } from "node:crypto";
import {
  type BigIntStats, chmodSync, closeSync, constants, copyFileSync, fchmodSync, fstatSync, fsyncSync, futimesSync, linkSync, lstatSync, mkdirSync,
  openSync, readFileSync, readSync, renameSync, rmdirSync, rmSync, unlinkSync, writeFileSync, writeSync,
} from "node:fs";
import { dirname, join } from "node:path";

import { Failure, fromNode, io, osError, sandboxError, valueError } from "./answers.js";
import { type Context, revisionOf } from "./operations.js";
import { inFolderRefusal, protectedInFolder } from "./protect.js";

// The most files one look answers for: its answer is one message.
const MAX_LOOKED = 2_000;
const PIECE_BYTES = 1024 * 1024;
const ID = /^[0-9a-f]{40}$/;
// A saga's id, as a folder's name in the app's data.
const SAGA = /^[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}$/;
// A file's revision, as the look answers it (operations.ts, revisionOf).
const REVISION = /^[0-9]+:[0-9]+:[0-9]+:-?[0-9]+:-?[0-9]+$/;

const BAD = "land takes revisions of paths, an apply or an unapply of a saga's step on a path, or the forgetting of a saga";

// Which file a name holds, by what does not change when it is moved or linked: a change of its bytes moves its size or its time.
interface Identity {
  dev: string;
  ino: string;
  size: string;
  mtimeNs: string;
}

// What a step did, written down before it does it.
interface Step {
  path: string;
  was: Identity | null; // the real file it found, or none
  wrote: Identity | null; // the file it put there, or none for a deletion
  mode: number | null; // the replaced file's
  made: string[]; // the folders it made for its file, deepest first
  above: Array<[string, number]>; // a deletion's: the folders above its file, each with its mode, for one it empties
  temp: string | null; // its new file's name beside the real one, until it is linked in
  aside: string | null; // the real file's, while it is moved aside
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
const plain = (st: BigIntStats): boolean => st.isFile() && st.nlink === 1n;
// A revision as an identity: its change time is left out, which a move changes.
function expectedIdentity(revision: string): Identity {
  const [dev = "", ino = "", size = "", mtimeNs = ""] = revision.split(":");
  return { dev, ino, size, mtimeNs };
}

function look(path: string): BigIntStats | null {
  try {
    return lstatSync(path, { bigint: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw fromNode(error, path);
  }
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

// The folders of *parts* under *root* that are not there yet. One on the way that is no real folder, a link among them, is refused.
function missing(root: string, parts: string[], path: string): string[] {
  const absent: string[] = [];
  let at = root;
  for (const part of parts.slice(0, -1)) {
    at = join(at, part);
    const found = absent.length ? null : look(at);
    if (found === null) absent.push(at);
    else if (!found.isDirectory()) throw sandboxError(`Not a path in this folder: '${path}'`);
  }
  return absent;
}

// What the look answers for a real file: its revision, "absent", or "other" for anything a landing does not replace.
function token(root: string, parts: string[], path: string): string {
  let absent: string[];
  try {
    absent = missing(root, parts, path);
  } catch {
    return "other";
  }
  if (absent.length) return "absent";
  const found = look(join(root, ...parts));
  if (found === null) return "absent";
  return plain(found) ? revisionOf(found) : "other";
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

function remove(path: string | null): void {
  if (path === null) return;
  try {
    unlinkSync(path);
  } catch {
    // Not there.
  }
}

// *fd*, a copy made across filesystems, given the times of the file at *from*, to the microsecond: a
// move keeps a file's times, and a put-back's file is the one the user saved then, not one made now.
function dated(fd: number, from: string): void {
  const { atimeNs, mtimeNs } = lstatSync(from, { bigint: true });
  futimesSync(fd, Number(atimeNs) / 1e9, Number(mtimeNs) / 1e9);
}

// *from*, a file of the folder's filesystem, at *to* in the app's data: moved, or copied where that is another filesystem.
function keep(from: string, to: string): void {
  try {
    renameSync(from, to);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EXDEV") throw fromNode(error, from);
    copyFileSync(from, to);
    // Read-only: a file the user may only read is copied with that mode, and is theirs to date and sync all the same.
    const fd = openSync(to, "r");
    try {
      dated(fd, from);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    unlinkSync(from);
  }
}

// *from* takes the name *target*, only where nothing holds it: a link, or a copy linked in where *from* is on another filesystem.
function place(from: string, target: string, mode: number | null): void {
  try {
    linkSync(from, target);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EXDEV") throw error;
    const staged = join(dirname(target), `.surogate-${randomUUID()}.tmp`);
    copyFileSync(from, staged, constants.COPYFILE_EXCL);
    try {
      const fd = openSync(staged, "r");
      try {
        if (mode !== null) fchmodSync(fd, mode);
        dated(fd, from);
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      linkSync(staged, target);
    } finally {
      remove(staged);
    }
  }
}

// The copy's file at *parts*, written beside *target* under a name of the landing's own: only a plain file of the
// copy's, no link on its way, whose bytes are the blob *after*. Its name, and which file it is.
function stage(copy: string, parts: string[], path: string, target: string, after: string, mode: number | null): { temp: string; wrote: Identity } {
  if (missing(copy, parts, path).length) throw osError("ENOENT", path);
  const source = join(copy, ...parts);
  let from: number;
  try {
    from = openSync(source, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  } catch (error) {
    // A link at its name is no file of the copy's.
    if ((error as NodeJS.ErrnoException).code === "ELOOP") throw sandboxError(`Not a path in this folder: '${path}'`);
    throw fromNode(error, path);
  }
  const temp = join(dirname(target), `.surogate-${randomUUID()}.tmp`);
  let to: number | undefined;
  try {
    const opened = fstatSync(from);
    if (!opened.isFile()) throw sandboxError(`Not a path in this folder: '${path}'`);
    const { size } = opened;
    to = io(path, () => openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o666));
    const hash = createHash("sha1").update(`blob ${size}\0`);
    const piece = Buffer.allocUnsafe(PIECE_BYTES);
    let read = 0;
    for (;;) {
      const count = io(path, () => readSync(from, piece, 0, piece.length, null));
      if (count === 0) break;
      read += count;
      hash.update(piece.subarray(0, count));
      for (let written = 0; written < count;) written += io(path, () => writeSync(to!, piece, written, count - written));
    }
    if (read !== size || hash.digest("hex") !== after) {
      throw new Failure({ type: "stale", message: `${path} changed in the thread's copy after its turn was committed, so it was not landed` });
    }
    if (mode !== null) fchmodSync(to, mode);
    fsyncSync(to);
    return { temp, wrote: identityOf(fstatSync(to, { bigint: true })) };
  } catch (error) {
    remove(temp);
    throw error;
  } finally {
    closeSync(from);
    if (to !== undefined) closeSync(to);
  }
}

class Landing {
  private readonly folder: string;
  private readonly copy: string;
  private readonly kept: string;

  constructor(context: Context, private readonly saga: string) {
    this.folder = context.folder;
    this.copy = context.landing!.copy;
    this.kept = join(context.landing!.kept, saga);
  }

  private record(step: number): string {
    return join(this.kept, `${step}.json`);
  }

  private bytes(step: number): string {
    return join(this.kept, String(step));
  }

  private write(step: number, done: Step): void {
    mkdirSync(this.kept, { recursive: true, mode: 0o700 });
    const temp = `${this.record(step)}.new`;
    const fd = openSync(temp, "w", 0o600);
    try {
      writeFileSync(fd, JSON.stringify(done));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(temp, this.record(step));
    syncDir(this.kept);
  }

  private read(step: number): Step | null {
    try {
      return JSON.parse(readFileSync(this.record(step), "utf8")) as Step;
    } catch {
      return null;
    }
  }

  // What a step kept goes: its record, and the file it replaced; and the saga's folder with its last step's.
  private drop(step: number): void {
    remove(this.record(step));
    remove(this.bytes(step));
    this.rmdir(this.kept);
  }

  apply(step: number, path: string, before: string | null, after: string | null, expected: string): unknown {
    const parts = partsOf(path);
    const target = join(this.folder, ...parts);
    if (protectedInFolder(this.folder, target)) throw sandboxError(inFolderRefusal(path));
    // The guest writes the place the copy is in: a link in the copy's stead would name any folder it likes.
    if (look(this.copy)?.isDirectory() === false) throw sandboxError("This thread's copy is not a folder of the app's own");
    const absent = missing(this.folder, parts, path);
    const done = { path, before, after };
    // A step that ran already, as after an answer that was lost: the real file is what it wrote.
    const earlier = this.read(step);
    if (earlier?.path === path && earlier.temp === null && same(look(target), earlier.wrote)) return { ...done, made: earlier.made.map((dir) => dir.slice(this.folder.length + 1)) };
    // The look, again, before anything is made: a file saved since is no file of this landing's.
    if (token(this.folder, parts, path) !== expected) throw conflict(path);
    const found = absent.length ? null : look(target);
    if (after === null && found === null) return { ...done, made: [] };
    // A landing deletes only what its thread deleted: a name its copy still holds is not the turn's
    // deletion, whoever asks for it.
    if (after === null && !missing(this.copy, parts, path).length && look(join(this.copy, ...parts)) !== null) {
      throw new Failure({ type: "stale", message: `${path} is still in the thread's copy, so it was not deleted from the folder` });
    }
    const made: string[] = [];
    // A deletion takes the folders it empties with it: each one's mode, for its put-back to make it again as it was.
    const above: Array<[string, number]> = [];
    for (let at = dirname(target); after === null && at !== this.folder && at.startsWith(`${this.folder}/`); at = dirname(at)) {
      above.push([at, Number(io(path, () => lstatSync(at)).mode & 0o7777)]);
    }
    let temp: string | null = null;
    let aside: string | null = null;
    try {
      for (const dir of absent) {
        io(path, () => mkdirSync(dir));
        made.unshift(dir);
      }
      const mode = found === null ? null : Number(found.mode & 0o7777n);
      const staged = after === null ? null : stage(this.copy, parts, path, target, after, mode);
      temp = staged?.temp ?? null;
      aside = found === null ? null : join(dirname(target), `.surogate-${randomUUID()}.tmp`);
      const did = { path, was: found === null ? null : identityOf(found), wrote: staged?.wrote ?? null, mode, made, above };
      this.write(step, { ...did, temp, aside });
      if (aside !== null) {
        io(path, () => renameSync(target, aside!));
        // Moved aside, no save by its name reaches it: it is the file the look saw, or it goes back.
        const moved = look(aside);
        if (moved === null || !plain(moved) || !same(moved, expectedIdentity(expected))) {
          this.back(aside, target);
          aside = null;
          throw conflict(path);
        }
      }
      if (temp !== null) {
        try {
          linkSync(temp, target);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw fromNode(error, path);
          // A file was made at its name meanwhile: that one stays, and the one moved aside was replaced by it.
          throw conflict(path);
        }
        remove(temp);
        temp = null;
      }
      if (aside !== null) {
        keep(aside, this.bytes(step));
        aside = null;
      }
      this.write(step, { ...did, temp: null, aside: null });
      if (after === null) this.empty(dirname(target));
      syncDir(dirname(target));
      return { ...done, made: made.map((dir) => dir.slice(this.folder.length + 1)) };
    } catch (error) {
      // Nothing of the step stays: its own file, the real one moved aside (a newer one at its name stays), the folders it made.
      remove(temp);
      if (aside !== null) this.back(aside, target);
      for (const dir of made) this.rmdir(dir);
      this.drop(step);
      throw error;
    }
  }

  // The real file moved aside takes its name again, unless a newer file holds it.
  private back(aside: string, target: string): void {
    try {
      linkSync(aside, target);
    } catch {
      // A file was saved at its name meanwhile: that one stays.
    }
    remove(aside);
  }

  private rmdir(dir: string): boolean {
    try {
      rmdirSync(dir);
      return true;
    } catch {
      return false;
    }
  }

  // The folders a deletion emptied go with it, up to the folder itself.
  private empty(dir: string): void {
    for (let at = dir; at !== this.folder && at.startsWith(`${this.folder}/`) && this.rmdir(at); at = dirname(at));
  }

  unapply(step: number, path: string): unknown {
    const parts = partsOf(path);
    const target = join(this.folder, ...parts);
    const did = this.read(step);
    if (did === null) return { path, put_back: false };
    if (did.path !== path) throw valueError(BAD);
    if (protectedInFolder(this.folder, target)) throw sandboxError(inFolderRefusal(path));
    const changed = new Failure({ type: "conflict", message: `${path} changed after the landing wrote it, so it was not put back` });
    // The file the step replaced: still moved aside beside the real one, or kept.
    const aside = did.aside !== null && look(did.aside) !== null ? did.aside : null;
    const replaced = aside ?? (look(this.bytes(step)) !== null ? this.bytes(step) : null);
    const now = missing(this.folder, parts, path).length ? null : look(target);
    if (did.wrote !== null && same(now, did.wrote)) {
      // Its own file is there: moved aside as the real one was, and looked at again before anything takes its place.
      const out = join(dirname(target), `.surogate-${randomUUID()}.tmp`);
      io(path, () => renameSync(target, out));
      if (!same(look(out), did.wrote)) {
        this.back(out, target);
        throw changed;
      }
      if (did.was !== null) {
        if (replaced === null) {
          this.back(out, target);
          throw changed;
        }
        try {
          place(replaced, target, did.mode);
        } catch (error) {
          remove(out);
          if ((error as NodeJS.ErrnoException).code === "EEXIST") throw changed;
          throw fromNode(error, path);
        }
      }
      remove(out);
    } else if (now === null && did.was !== null) {
      // A deletion, or a write cut short with the real file moved aside: the replaced file takes its name again.
      if (replaced === null) throw changed;
      const modes = new Map(did.above);
      for (const dir of missing(this.folder, parts, path)) {
        io(path, () => mkdirSync(dir));
        const mode = modes.get(dir);
        if (mode !== undefined) io(path, () => chmodSync(dir, mode));
      }
      try {
        place(replaced, target, did.mode);
      } catch (error) {
        throw (error as NodeJS.ErrnoException).code === "EEXIST" ? changed : fromNode(error, path);
      }
    } else if (!same(now, did.was)) {
      // Neither the step's file nor the one it found: someone else's change.
      throw changed;
    }
    remove(did.temp);
    remove(aside);
    if (did.wrote !== null && did.was === null) for (const dir of did.made) this.rmdir(dir);
    this.drop(step);
    syncDir(dirname(target));
    return { path, put_back: true };
  }

  forget(): unknown {
    rmSync(this.kept, { recursive: true, force: true });
    return {};
  }
}

/** The file helper's `land` kind: a landing's look at the real files, its applies and put-backs, and its forgetting. */
export function land(args: Record<string, unknown>, context: Context): unknown {
  // No helper but a landing's has a copy to land from.
  if (!context.landing) throw new Failure({ type: "unsupported", message: "This computer cannot do 'land' yet" });
  const { action, saga, step, path, before, after, expected, paths } = args;
  if (action === "revisions") {
    if (!Array.isArray(paths) || paths.length > MAX_LOOKED) throw valueError(BAD);
    return { revisions: paths.map((one) => [one, token(context.folder, partsOf(one), one as string)]) };
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
