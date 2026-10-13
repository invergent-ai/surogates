// The file operations, as surogates/devices/workspace.py defines them and the
// cloud's LocalWorkspaceIO does them, run by the file helper inside the folder's
// sandbox. Each is synchronous fs work, so one helper's changes to a path never
// interleave; only ripgrep waits on a process. One helper holds a folder at a
// time, but a chat bound to a folder nested inside another chat's is a second
// writer there, as an editor is. So a write that expects a revision checks it
// before it makes anything, and again just before its rename, which leaves
// another writer only microseconds.

import { type ChildProcess, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  accessSync, type BigIntStats, closeSync, constants, type Dirent, fchmodSync, fstatSync, readSync, type Stats, statSync, writeSync,
} from "node:fs";
import { constants as osConstants } from "node:os";
import { posix } from "node:path";

import { isBase64, type Outcome } from "../link/protocol.js";
import {
  conflict, Failure, fromNode, io, MAX_MESSAGE_CHARS, MAX_NAMES, MAX_PAYLOAD_BYTES, MAX_READ_BYTES, MAX_WALK_FILES,
  MAX_WALK_DEPTH, MAX_WALK_LOOKS, MAX_WRITE_BYTES, NUL_REFUSED, OUTPUT_CAP_CHARS, osError, pyJsonLength, READ_TOO_LARGE,
  sandboxError, SHOWN_DOT_FOLDERS, valueError, WALK_BUDGET_MS, WALK_MARGIN_NS, WRITE_TOO_LARGE,
} from "./answers.js";
import { retold, saidInFailing, searched, spared } from "./edge.js";
import { fdPaths, type Listing, type Where } from "./fd-paths.js";
import { FLAGS } from "./flags.js";
import { identityOf } from "./identity.js";
import { land } from "./land.js";
import { fileOs } from "./os.js";
import { type Edge, edgeOf, keyInFolder, resolveInFolder } from "./paths.js";
import { checkWrite, writeRefusal } from "./protect.js";

// A session's paths, POSIX on every OS (session-path.ts): files/ is on the agent disk, which holds no module of the
// app's but its own.
const { dirname, join, resolve } = posix;

export interface Context {
  folder: string; // resolved
  // A thread's copy alone: the path of the folder it is a copy of, by which every request and answer names its files
  // (spec, Section 13). The app's own, given once at the helper's start, never a request's. Without it, the folder is
  // named by its own path. A landing's helper has none: it works in the folder itself.
  at?: string;
  home: string;
  env: Record<string, string | undefined>;
  // A landing's helper alone (land.ts): the thread's copy its files come from, and where the files it
  // replaces are kept until the landing is recorded. The app's own paths, never a request's. A recovery's
  // helper is given where they are kept and no copy: it puts back what a landing cut short, and lands nothing.
  landing?: { copy?: string; kept: string };
}

type Kind = (args: Record<string, unknown>, context: Context, signal: AbortSignal) => unknown;

const KINDS: Record<string, Kind> = {
  resolve: (args, { folder, home, at }) => resolveInFolder(folder, home, text(args, "path"), at),
  check_write: (args, { folder, home, at }) => checkWrite(folder, home, text(args, "path"), at),
  stat,
  read,
  read_lines: readLines,
  write,
  delete: remove,
  list_dir: listDir,
  walk,
  ripgrep,
  land,
};

/** Every kind the helper has, by name: the table itself, for whoever must look at each one's answers. */
export const kinds = (): string[] => Object.keys(KINDS);

// The helper's own files beside a user's: a write's temp file, and a landing's (land.ts). A helper killed at the wrong
// moment leaves one, so no listing names it as a file of the folder's.
export const OWN_FILE = /^\.surogate-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.tmp$/;
export const ownFile = (): string => `.surogate-${randomUUID()}.tmp`;

const WRITE_EFBIG = new Failure({ type: "os", code: "EFBIG", message: WRITE_TOO_LARGE });
const READ_EFBIG = new Failure({ type: "os", code: "EFBIG", message: READ_TOO_LARGE });
// What one read call takes from the file at a time.
const READ_PIECE_BYTES = 1024 * 1024;
// The codecs read_file decodes text in, which read_lines pages, as surogates/tools/workspace_io/local.py's
// CODE_UNITS: each one's code unit, as its width in bytes and where in it a line feed's or carriage return's value is.
const CODE_UNITS: Record<string, readonly [width: number, low: number]> = {
  "utf-8": [1, 0], "utf-8-sig": [1, 0], "utf-16-le": [2, 0], "utf-16-be": [2, 1], "utf-32-le": [4, 0], "utf-32-be": [4, 3],
};
const UTF8_BOM = Buffer.from([0xef, 0xbb, 0xbf]);
export const BAD_PAGE =
  `read_lines takes one of the encodings read_file picks, an offset from 1, an integer limit and a max_bytes from 0 to ${MAX_PAYLOAD_BYTES}`;
export const BAD_WALK =
  "walk takes a key, the folder names it skips anywhere and directly under the key, whether it skips hidden folders, "
  + "and a since an earlier walk gave, or null";
const names = (value: unknown): value is string[] => Array.isArray(value) && value.every((name) => typeof name === "string");

export const RG_MISSING =
  "ripgrep (rg) not found on PATH -- install it (apt/brew/dnf install ripgrep) on this computer";
const UTF8 = new TextDecoder("utf-8", { ignoreBOM: true }); // Python keeps a leading BOM
const STDERR_BYTES = 1024; // the message keeps 200 code points, at most 800 bytes
const CANCELLED = new Failure({ type: "cancelled", message: "The session stopped this search" });
const NARROW = new Failure({
  type: "ripgrep",
  message: `search output over ${OUTPUT_CAP_CHARS} characters; narrow the pattern, path or glob`,
});

// One operation's outcome. Never rejects: whatever goes wrong is an error outcome.
export async function perform(
  kind: string, args: Record<string, unknown>, context: Context, signal: AbortSignal,
): Promise<Outcome> {
  const handle = Object.hasOwn(KINDS, kind) ? KINDS[kind] : undefined;
  if (!handle) return { error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } };
  let outcome: Outcome;
  try {
    outcome = { ok: (await handle(args, context, signal)) ?? null };
  } catch (error) {
    const refusal = error instanceof Failure
      ? error.refusal
      : { type: "other", message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) };
    // A copy's helper answers in the words of the folder it is a copy of, whatever kind failed and however.
    const edge = edgeOf(context.folder, context.at ?? context.folder);
    outcome = { error: edge ? retold(refusal, error, edge) : refusal };
  }
  // A read is bounded by MAX_READ_BYTES instead: its data over MAX_PAYLOAD_BYTES goes as a transfer.
  if (kind !== "read" && JSON.stringify(outcome).length > MAX_MESSAGE_CHARS) {
    return { error: { type: "too_large", message: `The result of ${kind} is too large` } };
  }
  return outcome;
}

function text(args: Record<string, unknown>, name: string): string {
  const value = args[name];
  if (typeof value !== "string") throw valueError(`'${name}' must be a string`);
  return value;
}

function whole(args: Record<string, unknown>, name: string): number {
  const value = args[name];
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw valueError(`'${name}' must be a whole number`);
  }
  return value;
}

function wholeOrNull(args: Record<string, unknown>, name: string): number | null {
  return args[name] === null ? null : whole(args, name);
}

function textOrNull(args: Record<string, unknown>, name: string): string | null {
  return args[name] === null ? null : text(args, name);
}

// This version of the file, as LocalWorkspaceIO.stat names it (surogates/tools/workspace_io/local.py):
// the ctime moves with every change, and utimes cannot set it back. dev and ino are unsigned there, and
// Node reads them from a signed array, so an inode at or above 2^63 is put back to the number Python gives.
export const revisionOf = (st: BigIntStats): string => {
  const { dev, ino } = identityOf(st);
  return `${BigInt.asUintN(64, dev)}:${BigInt.asUintN(64, ino)}:${st.size}:${st.mtimeNs}:${st.ctimeNs}`;
};

// The file's revision now, or null when it cannot be stat'ed.
function revisionAt(key: string): string | null {
  try {
    return revisionOf(fdPaths.stat(key, { bigint: true }));
  } catch {
    return null;
  }
}

function stat(args: Record<string, unknown>, { folder, at }: Context): unknown {
  try {
    const st = fdPaths.stat(keyInFolder(folder, text(args, "key"), at), { bigint: true });
    // st_mtime as CPython computes it from tv_sec and tv_nsec: seconds plus
    // nanoseconds * 1e-9, with tv_nsec never negative.
    let seconds = st.mtimeNs / 1_000_000_000n;
    let nanoseconds = st.mtimeNs % 1_000_000_000n;
    if (nanoseconds < 0n) {
      seconds -= 1n;
      nanoseconds += 1_000_000_000n;
    }
    return {
      is_dir: st.isDirectory(), size: Number(st.size), mtime: Number(seconds) + Number(nanoseconds) * 1e-9,
      revision: revisionOf(st),
    };
  } catch {
    return null;
  }
}

// An open file that is a regular file; refused otherwise, before any byte moves.
function regular(fd: number, key: string): Stats {
  const st = fstatSync(fd);
  if (st.isDirectory()) throw osError("EISDIR", key);
  if (!st.isFile()) throw osError("EINVAL", key, "Not a regular file");
  return st;
}

function read(args: Record<string, unknown>, { folder, at }: Context): string {
  const key = keyInFolder(folder, text(args, "key"), at);
  const wanted = wholeOrNull(args, "max_bytes");
  const limit = wanted === null ? MAX_READ_BYTES + 1 : Math.min(wanted, MAX_READ_BYTES + 1);
  const fd = io(key, () => fdPaths.open(key, constants.O_RDONLY | FLAGS.NOFOLLOW | constants.O_NONBLOCK));
  try {
    // Asked for whole, a file over the cap is refused from its size, before a byte of it is read.
    if (regular(fd, key).size > MAX_READ_BYTES && limit > MAX_READ_BYTES) throw READ_EFBIG;
    // In pieces, as far as the file goes: a small file costs one piece, not the cap.
    const pieces: Buffer[] = [];
    let size = 0;
    while (size < limit) {
      const piece = Buffer.allocUnsafe(Math.min(READ_PIECE_BYTES, limit - size));
      const count = io(key, () => readSync(fd, piece, 0, piece.length, null));
      if (count === 0) break;
      // A short read keeps a copy of what it read, not the whole piece.
      pieces.push(count === piece.length ? piece : Buffer.from(piece.subarray(0, count)));
      size += count;
    }
    if (size > MAX_READ_BYTES) throw READ_EFBIG;
    return Buffer.concat(pieces, size).toString("base64");
  } finally {
    io(key, () => closeSync(fd));
  }
}

const isInteger = (value: unknown): value is number => Number.isInteger(value);

// A page of a text file, as WorkspaceIO.read_lines defines it (surogates/tools/workspace_io/base.py): the bytes of the
// whole lines Python's lines[offset - 1:min(offset - 1 + limit, total)] selects that fit in max_bytes, or, when not
// even the first does, its first max_bytes; and how many lines the file has. Only line ends are found here: the cloud
// decodes the page and applies every rule.
function readLines(args: Record<string, unknown>, { folder, at }: Context): { data: string; total_lines: number } {
  const key = keyInFolder(folder, text(args, "key"), at);
  const { encoding, offset, limit, max_bytes: maxBytes } = args;
  const unit = typeof encoding === "string" && Object.hasOwn(CODE_UNITS, encoding) ? CODE_UNITS[encoding] : undefined;
  if (
    !unit || !isInteger(offset) || offset < 1 || !isInteger(limit)
    || !isInteger(maxBytes) || maxBytes < 0 || maxBytes > MAX_PAYLOAD_BYTES
  ) {
    throw valueError(BAD_PAGE);
  }
  const fd = io(key, () => fdPaths.open(key, constants.O_RDONLY | FLAGS.NOFOLLOW | constants.O_NONBLOCK));
  try {
    // From the file's size, before a byte of it is read.
    if (regular(fd, key).size > MAX_READ_BYTES) throw READ_EFBIG;
    const page = pageOf(fd, key, unit, encoding === "utf-8-sig", offset, limit, maxBytes);
    return { data: page.data.toString("base64"), total_lines: page.total };
  } finally {
    io(key, () => closeSync(fd));
  }
}

// Fills *buffer* from *position* until it is full or the file ends: some filesystems answer less than asked.
function fill(fd: number, key: string, buffer: Buffer, position: number): number {
  let count = 0;
  while (count < buffer.length) {
    const got = io(key, () => readSync(fd, buffer, count, buffer.length - count, position + count));
    if (got === 0) break;
    count += got;
  }
  return count;
}

// One pass over the file in pieces. Its lines end as Python's readlines() ends them, at a line feed, a carriage return
// or both, as code units of the encoding; candidates are found by memchr (indexOf) and checked as units.
function pageOf(
  fd: number, key: string, [width, low]: readonly [number, number], skipBom: boolean,
  offset: number, limit: number, maxBytes: number,
): { data: Buffer; total: number } {
  const bom = Buffer.alloc(3);
  // utf-8-sig drops its BOM, so line 1 starts after it. The other codecs keep a BOM as a character of line 1.
  const start = skipBom && fill(fd, key, bom, 0) === 3 && bom.equals(UTF8_BOM) ? 3 : 0;
  // The unit at *at* in *bytes*: a line feed (10), a carriage return (13), or neither (0).
  const unitAt = (bytes: Buffer, at: number): number => {
    const value = bytes[at + low];
    if (value !== 10 && value !== 13) return 0;
    for (let byte = 0; byte < width; byte++) if (byte !== low && bytes[at + byte] !== 0) return 0;
    return value;
  };
  const first = offset - 1;
  const buffer = Buffer.allocUnsafe(READ_PIECE_BYTES);
  const peek = Buffer.alloc(width);
  const fits: number[] = []; // where each line from the first ends, while the page holds it
  let total = 0; // line ends so far
  let begin = first === 0 ? start : -1; // where the first line starts
  const takes = (end: number) => end - begin <= maxBytes && (limit <= 0 || fits.length < limit);
  let taking = true;
  let at = start; // where the piece starts in the file
  let last = start; // where the last line end so far ends
  for (;;) {
    let count = fill(fd, key, buffer, at);
    if (count === 0) break;
    // A growing file's end can cut a unit for a moment: the next piece starts on a whole one.
    if (count % width) count += fill(fd, key, buffer.subarray(count, count + width - (count % width)), at + count);
    const piece = buffer.subarray(0, count);
    // A unit cut off can now only be the file's last bytes.
    const units = count - (count % width);
    let size = count;
    let lf = piece.indexOf(10, low);
    let cr = piece.indexOf(13, low);
    for (;;) {
      const found = lf === -1 ? cr : cr === -1 ? lf : Math.min(lf, cr);
      const i = found - low;
      if (found === -1 || i + width > units) break;
      const value = i % width === 0 ? unitAt(piece, i) : 0;
      if (value === 0) {
        if (found === lf) lf = piece.indexOf(10, found + 1);
        else cr = piece.indexOf(13, found + 1);
        continue;
      }
      let end = i + width;
      if (value === 13) {
        // CR LF is one line end, also across two pieces.
        if (end < units) {
          if (unitAt(piece, end) === 10) end += width;
        } else if (fill(fd, key, peek, at + end) === width && unitAt(peek, 0) === 10) {
          end += width;
          size = end;
        }
      }
      if (lf !== -1 && lf < end + low) lf = piece.indexOf(10, end + low);
      if (cr !== -1 && cr < end + low) cr = piece.indexOf(13, end + low);
      total += 1;
      last = at + end;
      if (total === first) begin = last;
      else if (begin >= 0 && taking) {
        if (takes(last)) fits.push(last);
        else taking = false;
      }
    }
    at += size;
  }
  if (at > last) {
    // Bytes after the last line end are one more line.
    total += 1;
    if (taking && begin >= 0 && total > first && takes(at)) fits.push(at);
  }
  // Python's stop of lines[first:min(first + limit, total)], a limit below one included.
  let stop = Math.min(first + limit, total);
  if (stop < 0) stop = Math.max(stop + total, 0);
  if (first >= total || stop <= first) return { data: Buffer.alloc(0), total };
  const end = fits[Math.min(stop - first, fits.length) - 1];
  // No whole line fits: the first one's first max_bytes, for the cloud to cut.
  const data = Buffer.allocUnsafe(end === undefined ? maxBytes : end - begin);
  return { data: data.subarray(0, fill(fd, key, data, begin)), total };
}

// Temp file and atomic rename, keeping the replaced file's mode, as the cloud does.
function write(args: Record<string, unknown>, { folder, at }: Context): null {
  const key = keyInFolder(folder, text(args, "key"), at);
  // Only the in-folder protected names are re-checked here. A key is proven to be inside the folder; a folder that
  // holds the home folder's credentials is refused at host start, and the system paths on the cloud's list are
  // left to the operating system's own permissions.
  const refusal = writeRefusal(folder, key);
  if (refusal !== null) throw sandboxError(refusal);
  // Whether it may only make a file: said as true or false, or not at all. Any other word is refused, not
  // taken for a write that may replace what is there.
  if (args.create !== undefined && typeof args.create !== "boolean") throw valueError("'create' must be true or false");
  const encoded = text(args, "data");
  // Up to 50 MiB: a write's data that came in a transfer reaches the helper inline, once whole.
  if (encoded.length > Math.ceil(MAX_WRITE_BYTES / 3) * 4) throw WRITE_EFBIG;
  if (!isBase64(encoded)) throw valueError("data is not standard padded base64");
  const data = Buffer.from(encoded, "base64");
  if (data.length > MAX_WRITE_BYTES) throw WRITE_EFBIG;
  // The desktop's own, for what it saves by itself, as a page's download: the server's writes carry none.
  if (args.create === true) return create(key, data);
  // The revision this call's stat saw: anything else there, or nothing, is a conflict. Before anything is made.
  const expected = args.expected_revision;
  const check = () => {
    if (expected !== undefined && expected !== null && revisionAt(key) !== expected) throw conflict(key);
  };
  check();
  const parent = dirname(key);
  makeDirs(parent);
  // The rename replaces the name and never opens the file, so a file this user
  // cannot read is still replaced, as in the cloud.
  let existing: BigIntStats | undefined;
  try {
    existing = fdPaths.lstat(key);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") io(key, () => { throw error; });
  }
  let mode: number | null = null;
  if (existing) {
    if (existing.isDirectory()) throw osError("EISDIR", key);
    if (!existing.isFile()) throw osError("EINVAL", key, "Not a regular file");
    if (existing.nlink > 1n) throw osError("EMLINK", key, "File has more than one hard link, so it is not changed");
    mode = Number(existing.mode) & 0o7777;
  }
  const temporary = join(parent, ownFile());
  const fd = io(key, () =>
    fdPaths.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o666));
  try {
    try {
      for (let written = 0; written < data.length;) written += io(key, () => writeSync(fd, data, written));
      if (mode !== null) {
        try {
          fchmodSync(fd, mode);
        } catch {
          // The cloud writes on without the mode.
        }
      }
    } finally {
      io(key, () => closeSync(fd));
    }
    // Again, now that the temp file is whole: another writer may have changed the file while it was written.
    check();
    io(key, () => fdPaths.rename(temporary, key));
  } catch (error) {
    try {
      fdPaths.unlink(temporary);
    } catch {
      // Already gone.
    }
    throw error;
  }
  return null;
}

// A file made only where nothing is: its name is opened create-only, so a file or a folder there
// already is EEXIST, the look and the making are one act, and no temp file is renamed over what
// another writer made meanwhile. What it made goes when the data cannot be written whole.
function create(key: string, data: Buffer): null {
  makeDirs(dirname(key));
  const fd = io(key, () => fdPaths.open(key, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o666));
  try {
    try {
      for (let written = 0; written < data.length;) written += io(key, () => writeSync(fd, data, written));
    } finally {
      io(key, () => closeSync(fd));
    }
  } catch (error) {
    try {
      fdPaths.unlink(key);
    } catch {
      // Already gone.
    }
    throw error;
  }
  return null;
}

function remove(args: Record<string, unknown>, { folder, at }: Context): null {
  const key = keyInFolder(folder, text(args, "key"), at);
  const refusal = writeRefusal(folder, key);
  if (refusal !== null) throw sandboxError(refusal);
  if (key === folder) throw osError("EISDIR", key);
  io(key, () => {
    try {
      fdPaths.unlink(key);
    } catch (error) {
      // Darwin's unlink(2) refuses a folder with EPERM, where Linux's, and the cloud's, says EISDIR.
      if ((error as NodeJS.ErrnoException).code === "EPERM" && isFolder(key)) throw osError("EISDIR", key);
      throw error;
    }
  });
  return null;
}

// Whether *path* is a folder now, a link to one being none; false where it cannot be told.
function isFolder(path: string): boolean {
  try {
    return fdPaths.lstat(path).isDirectory();
  } catch {
    return false;
  }
}

// os.makedirs(dir, exist_ok=True): each missing parent first, so an error names
// the first folder that could not be made, as Python's does.
function makeDirs(dir: string): void {
  const parent = dirname(dir);
  if (parent !== dir && !fdPaths.exists(parent)) {
    try {
      makeDirs(parent);
    } catch (error) {
      if (!(error instanceof Failure && error.refusal.code === "EEXIST")) throw error;
    }
  }
  try {
    fdPaths.mkdir(dir);
  } catch (error) {
    let folderAlready = false;
    try {
      folderAlready = fdPaths.stat(dir).isDirectory();
    } catch {
      // Not a folder.
    }
    if (!folderAlready) io(dir, () => { throw error; });
  }
}

function listDir(args: Record<string, unknown>, { folder, at }: Context): string[] {
  const key = keyInFolder(folder, text(args, "key"), at);
  return io(key, () => fdPaths.readdir(key)).filter((name) => !OWN_FILE.test(name)).slice(0, MAX_NAMES);
}

// walk (surogates/devices/workspace.py): the regular files under the folder at the key, each as its path from it and
// its size, depth first. No link is followed, and no folder the tree hides is entered: one named in skip, one directly
// under the key named in skip_top, and with skip_hidden a dot-folder other than SHOWN_DOT_FOLDERS. A name that is not
// UTF-8 is left out: read as bytes, it does not survive the round trip, and its decoded twin could be another file.
// So is a file of the helper's own (OWN_FILE), as it is from list_dir's names.
// Since a cursor, only the files whose mtime or ctime is at or after it. The cursor is this computer's clock as the
// walk began, less WALK_MARGIN_NS. It stops after WALK_BUDGET_MS: every other operation on the folder waits for it.
//
// Each folder is entered through a handle on its parent, never by its path, and never through a link: a command in
// the VM can swap a folder for a link between the walk seeing it and entering it. Depth first, each folder entered as
// it is met, so a handle is held for each folder above the one being read, and no more: MAX_WALK_DEPTH of them at
// most. Each folder is read an entry at a time, so one of a million entries costs no more than the walk looks at. A
// folder it may not read, or one a link took the place of, is left out, and the walk goes on. Out of handles, it
// stops, said truncated: what the rest holds is unknown, and the walk must not be taken for whole.
function walk(args: Record<string, unknown>, { folder, at }: Context): { files: Array<[string, number]>; truncated: boolean; cursor: string } {
  const key = keyInFolder(folder, text(args, "key"), at);
  const { skip, skip_top: top, skip_hidden: hidden, since } = args;
  if (
    !names(skip) || !names(top) || typeof hidden !== "boolean"
    // A cursor is the clock in nanoseconds: 20 digits last past the year 5000.
    || !(since === null || (typeof since === "string" && /^[0-9]{1,20}$/.test(since)))
  ) {
    throw valueError(BAD_WALK);
  }
  const cursor = String(BigInt(Date.now()) * 1_000_000n - WALK_MARGIN_NS);
  const deadline = performance.now() + WALK_BUDGET_MS;
  const after = since === null ? null : BigInt(since);
  const skipped = new Set<string>(skip);
  const skippedTop = new Set<string>(top);
  const files: Array<[string, number]> = [];
  let cost = 2; // "[]"
  let looks = 0;
  let truncated = false;
  // The key's own failure is the answer.
  const keyFd = io(key, () => fdPaths.open(key, constants.O_RDONLY | FLAGS.DIRECTORY | FLAGS.NOFOLLOW));
  let keyDir: Listing;
  try {
    keyDir = io(key, () => fdPaths.listing(keyFd));
  } catch (error) {
    closeSync(keyFd);
    throw error;
  }
  const levels: Level[] = [{ fd: keyFd, rel: "", dir: keyDir }];
  try {
    while (levels.length > 0 && !truncated) {
      const level = levels.at(-1) as Level;
      let entry: Dirent<Buffer> | null;
      try {
        entry = level.dir.readSync();
      } catch {
        entry = null; // what it could not read of a folder is left out
      }
      if (entry === null) {
        levels.pop();
        close(level);
        continue;
      }
      looks += 1;
      if (looks > MAX_WALK_LOOKS || performance.now() > deadline) {
        truncated = true;
        break;
      }
      const name = entry.name.toString("utf8");
      if (!Buffer.from(name, "utf8").equals(entry.name)) continue;
      const path = level.rel ? `${level.rel}/${name}` : name;
      // The entry by its parent's handle: no link on the way to it is followed.
      const at: Where = { dir: level.fd, name };
      if (entry.isDirectory()) {
        const hides = hidden && name.startsWith(".") && !SHOWN_DOT_FOLDERS.has(name);
        if (skipped.has(name) || (!level.rel && skippedTop.has(name)) || hides) continue;
        // The key is the first level: this folder would be levels.length below it.
        if (levels.length > MAX_WALK_DEPTH) {
          truncated = true;
          break;
        }
        let fd: number;
        try {
          fd = fdPaths.open(at, constants.O_RDONLY | FLAGS.DIRECTORY | FLAGS.NOFOLLOW);
        } catch (error) {
          if (outOfHandles(error)) {
            truncated = true;
            break;
          }
          continue;
        }
        try {
          levels.push({ fd, rel: path, dir: fdPaths.listing(fd) });
        } catch (error) {
          closeSync(fd);
          if (outOfHandles(error)) {
            truncated = true;
            break;
          }
        }
        continue;
      }
      if (!entry.isFile() || OWN_FILE.test(name)) continue;
      let st: BigIntStats;
      try {
        st = fdPaths.lstat(at);
      } catch {
        continue;
      }
      if (after !== null && st.mtimeNs < after && st.ctimeNs < after) continue;
      const more = pyJsonLength(path) + 24;
      if (files.length === MAX_WALK_FILES || cost + more > MAX_PAYLOAD_BYTES) {
        truncated = true;
        break;
      }
      files.push([path, Number(st.size)]);
      cost += more;
    }
  } finally {
    for (const level of levels) close(level);
  }
  return { files, truncated, cursor };
}

// The process, or the system, has no file handle left: what a walk has not reached is unknown. Any other failure to
// enter a folder (EACCES, EPERM, ELOOP, ENOTDIR) is that folder's alone.
const outOfHandles = (error: unknown): boolean =>
  error instanceof Error && ["EMFILE", "ENFILE"].includes((error as NodeJS.ErrnoException).code ?? "");

// A folder the walk is in: its handle, its path from the key, and its entries, read as it goes.
interface Level {
  fd: number;
  rel: string;
  dir: Listing;
}

function close(level: Level): void {
  level.dir.closeSync();
  closeSync(level.fd);
}

// shutil.which: a name with a slash is checked as it is (relative to *cwd*);
// otherwise the first PATH entry holding an executable file of that name, a
// relative or empty entry being read from *cwd*. The guest's root runner answers
// which with it, in the commands' environment; here it finds the helper's rg.
export function findOnPath(name: string, path: string | undefined, cwd: string): string | null {
  if (!name || name.includes("\0")) return null;
  if (name.includes("/")) return runnable(name.startsWith("/") ? name : join(cwd, name)) ? name : null;
  const entries = path ?? "/bin:/usr/bin";
  if (!entries) return null;
  for (const entry of entries.split(":")) {
    const candidate = join(resolve(cwd, entry), name);
    if (runnable(candidate)) return candidate;
  }
  return null;
}

function runnable(path: string): boolean {
  try {
    if (statSync(path).isDirectory()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// A file of the helper's own beside a real one (OWN_FILE), as a glob of rg's: rg takes the last glob that matches a file,
// so given after the caller's it keeps every one of them out, as list_dir and walk do. One of the user's whose name only
// begins and ends like it is theirs.
const NOT_OWN = "!.surogate-????????-????-????-????-????????????.tmp";

// The cloud's command line: --no-ignore, -e so a pattern may start with "-", and no file of the helper's own. Where rg
// walks the folder (Linux, macOS, in the sandbox) it is given the folder and its own -g globs; where it does not
// (Windows), the helper lists the tree itself and gives rg only files, never a folder, with the glob already applied
// (fd-paths-win32.ts, searchFiles; fileOs.rgWalks).
async function ripgrep(args: Record<string, unknown>, { env, folder, at }: Context, signal: AbortSignal): Promise<string> {
  const key = keyInFolder(folder, text(args, "key"), at);
  const mode = text(args, "mode");
  if (mode !== "files" && mode !== "count" && mode !== "json") throw valueError(`unknown search mode '${mode}'`);
  const pattern = text(args, "pattern");
  const glob = textOrNull(args, "glob");
  const lines = whole(args, "context");
  if (pattern.includes("\0") || glob?.includes("\0")) throw valueError(NUL_REFUSED);
  const rg = fileOs.rg(env.PATH, folder, findOnPath);
  if (!rg) throw new Failure({ type: "ripgrep", message: RG_MISSING });
  // A user's rg config could change the results or add --pre, which runs a program.
  const { RIPGREP_CONFIG_PATH: _config, ...clean } = env;

  if (fileOs.rgWalks) {
    const argv = ["--no-ignore"];
    if (mode === "files") {
      argv.push("--files", "-g", pattern, "-g", NOT_OWN);
    } else {
      if (glob) argv.push("-g", glob);
      argv.push("-g", NOT_OWN);
      if (mode === "count") argv.push("-c");
      else argv.push("--json", ...(lines > 0 ? ["-C", String(lines)] : []));
      argv.push("-e", pattern);
    }
    // rg opens the key by its path, so it is given the path this computer has for it, held there until rg ends: the key
    // itself, in the sandbox that binds the folder. A folder that held nothing has nothing to find.
    const outside = io(key, () => fdPaths.outside(key));
    if (outside.empty) {
      outside.release();
      return "";
    }
    argv.push(...fileOs.rgArgs, outside.path);
    // rg names what it finds from the path it is given: on a copy, by the copy's path.
    const named = edgeOf(outside.path.slice(0, outside.path.length - (key.length - folder.length)), at ?? folder);
    const edge = named && fileOs.rgPrinted ? { ...named, printed: fileOs.rgPrinted } : named;
    let used = false;
    const nextBatch = () => (used ? null : ((used = true), { argv, release: () => {} }));
    return runSearch(rg, nextBatch, clean, signal, edge && { mode, edge }).finally(outside.release);
  }

  // Windows: rg is given explicit files, never a folder (it would walk one and follow a link swapped in behind it).
  const base = [...fileOs.rgArgs, "--no-ignore"];
  if (mode === "files") base.push("--files");
  else {
    // --with-filename: rg names the file in every line, as it does walking a folder; given one explicit file it would
    // otherwise print the count or the match with no path, and the answer is keyed by path (--json always names it).
    if (mode === "count") base.push("-c", "--with-filename");
    else base.push("--json", ...(lines > 0 ? ["-C", String(lines)] : []));
    base.push("-e", pattern);
  }
  const search = io(key, () => fdPaths.searchFiles(key, { filesMode: mode === "files", glob: mode === "files" ? pattern : glob, own: OWN_FILE }));
  // rg names each file by this computer's path for it; the folder's own path maps those back to its key space.
  const named = edgeOf(search.root, at ?? folder);
  const edge = named && fileOs.rgPrinted ? { ...named, printed: fileOs.rgPrinted } : named;
  // Each batch's files are given to rg after --, so no path is taken for an option, and are held while rg runs on them.
  const nextBatch = () => {
    const batch = search.next();
    return batch && { argv: [...base, "--", ...batch.paths], release: batch.release };
  };
  try {
    return await runSearch(rg, nextBatch, clean, signal, edge && { mode, edge });
  } finally {
    search.close();
  }
}

// ENOENT is no rg to run (or no interpreter for it); any other spawn error
// (E2BIG, EACCES, EMFILE) is an os error naming rg.
function spawnFailure(error: unknown, rg: string): unknown {
  return (error as NodeJS.ErrnoException | undefined)?.code === "ENOENT"
    ? new Failure({ type: "ripgrep", message: RG_MISSING })
    : fromNode(error, rg);
}

// A batch of a search: rg's whole command line for it, and what to let go of once rg has run on it. A search is one
// batch where rg walks the folder (Linux, macOS), or a batch of pinned files at a time where the helper lists the tree
// (Windows); *nextBatch* gives the next, or null at the end. The batches' outputs join, as rg's one walk's lines would.
interface Batch {
  argv: string[];
  release(): void;
}

// *copy*: a search of a thread's copy, or on Windows of the folder by its own path, answered by the folder's path
// (edge.ts). rg runs on each batch in turn; its output, its errors and the cap carry across them, and the edge maps the
// whole once it is done.
function runSearch(
  rg: string, nextBatch: () => Batch | null, env: Record<string, string | undefined>, signal: AbortSignal, copy?: { mode: string; edge: Edge },
): Promise<string> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(CANCELLED);
      return;
    }
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let size = 0;
    let errSize = 0;
    let over = false;
    // What each line of a copy's search loses of its length as it is answered, and the lines so far, the one being written too.
    const spare = copy ? spared(copy.mode, copy.edge) : 0;
    let lines = 1;
    let child: ChildProcess | null = null;
    let done = false;
    const kill = () => {
      try {
        child?.kill("SIGKILL");
      } catch {
        // Already gone.
      }
    };
    const settle = (fn: () => void) => {
      if (done) return;
      done = true;
      signal.removeEventListener("abort", onAbort);
      fn();
    };
    function onAbort() {
      kill();
      settle(() => reject(CANCELLED));
    }
    signal.addEventListener("abort", onAbort);
    const finish = () => {
      const found = copy ? searched(copy.mode, UTF8.decode(Buffer.concat(out)), copy.edge) : UTF8.decode(Buffer.concat(out));
      if (pyJsonLength(found) > OUTPUT_CAP_CHARS) settle(() => reject(NARROW));
      else settle(() => resolve(found));
    };
    const pull = () => {
      const batch = nextBatch();
      if (!batch) {
        finish();
        return;
      }
      run(batch);
    };
    function run(batch: Batch) {
      try {
        // With no console window on Windows, where the helper has none to give it (Node, child_process, windowsHide).
        child = spawn(rg, batch.argv, { env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
      } catch (error) {
        batch.release();
        settle(() => reject(spawnFailure(error, rg)));
        return;
      }
      // At once: an error event nobody listens for crashes the helper.
      child.on("error", (error) => {
        batch.release();
        settle(() => reject(spawnFailure(error, rg)));
      });
      // A child that could not start (EMFILE) has no streams.
      child.stdout?.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (spare > 0) for (const byte of chunk) if (byte === 10) lines += 1;
        // Every byte costs at least one character on the wire, so past the cap in
        // bytes the answer is already known.
        if (size - lines * spare > OUTPUT_CAP_CHARS) {
          over = true;
          kill();
        } else {
          out.push(chunk);
        }
      });
      child.stderr?.on("data", (chunk: Buffer) => {
        if (errSize < STDERR_BYTES) err.push(chunk.subarray(0, STDERR_BYTES - errSize));
        errSize += chunk.length;
      });
      child.on("close", (code, killedBy) => {
        batch.release();
        if (done) return;
        if (signal.aborted) {
          settle(() => reject(CANCELLED));
          return;
        }
        if (over) {
          settle(() => reject(NARROW));
          return;
        }
        const status = code ?? -(killedBy ? osConstants.signals[killedBy] : 0);
        if (status !== 0 && status !== 1) {
          const wrote = copy
            ? saidInFailing(Buffer.concat(err), errSize > STDERR_BYTES, (bytes) => UTF8.decode(bytes), copy.edge)
            : UTF8.decode(Buffer.concat(err));
          const refusal = { type: "ripgrep", message: `rg exited ${status}: ${[...wrote].slice(0, 200).join("")}` };
          settle(() => reject(new Failure(refusal, copy && (() => refusal))));
          return;
        }
        pull();
      });
    }
    pull();
  });
}
