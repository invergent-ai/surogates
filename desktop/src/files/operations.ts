// The file operations, as surogates/devices/workspace.py defines them and the
// cloud's LocalWorkspaceIO does them, run by the file helper inside the folder's
// sandbox. Each is synchronous fs work, so two changes to one path never
// interleave; only ripgrep waits on a process.

import { type ChildProcess, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  accessSync, type BigIntStats, closeSync, constants, existsSync, fchmodSync, fstatSync, lstatSync, mkdirSync, openSync,
  readdirSync, readSync, renameSync, type Stats, statSync, unlinkSync, writeSync,
} from "node:fs";
import { constants as osConstants } from "node:os";
import { dirname, join, resolve } from "node:path";

import { isBase64, type Outcome } from "../link/protocol.js";
import {
  Failure, fromNode, io, MAX_MESSAGE_CHARS, MAX_NAMES, MAX_PAYLOAD_BYTES, MAX_READ_BYTES, MAX_WRITE_BYTES,
  OUTPUT_CAP_CHARS, osError, pyJsonLength, READ_TOO_LARGE, sandboxError, valueError, WRITE_TOO_LARGE,
} from "./answers.js";
import { keyInFolder, resolveInFolder } from "./paths.js";
import { checkWrite, inFolderRefusal, protectedInFolder } from "./protect.js";

export interface Context {
  folder: string; // resolved
  home: string;
  env: Record<string, string | undefined>;
}

type Kind = (args: Record<string, unknown>, context: Context, signal: AbortSignal) => unknown;

const KINDS: Record<string, Kind> = {
  resolve: (args, { folder, home }) => resolveInFolder(folder, home, text(args, "path")),
  check_write: (args, { folder, home }) => checkWrite(folder, home, text(args, "path")),
  stat,
  read,
  read_lines: readLines,
  write,
  delete: remove,
  list_dir: listDir,
  ripgrep,
  which,
};

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
    outcome = {
      error: error instanceof Failure
        ? error.refusal
        : { type: "other", message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) },
    };
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
// the ctime moves with every change, and utimes cannot set it back.
const revisionOf = (st: BigIntStats): string => `${st.dev}:${st.ino}:${st.size}:${st.mtimeNs}:${st.ctimeNs}`;

function stat(args: Record<string, unknown>, { folder }: Context): unknown {
  try {
    const st = statSync(keyInFolder(folder, text(args, "key")), { bigint: true });
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

function read(args: Record<string, unknown>, { folder }: Context): string {
  const key = keyInFolder(folder, text(args, "key"));
  const wanted = wholeOrNull(args, "max_bytes");
  const limit = wanted === null ? MAX_READ_BYTES + 1 : Math.min(wanted, MAX_READ_BYTES + 1);
  const fd = io(key, () => openSync(key, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK));
  try {
    regular(fd, key);
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
function readLines(args: Record<string, unknown>, { folder }: Context): { data: string; total_lines: number } {
  const key = keyInFolder(folder, text(args, "key"));
  const { encoding, offset, limit, max_bytes: maxBytes } = args;
  const unit = typeof encoding === "string" && Object.hasOwn(CODE_UNITS, encoding) ? CODE_UNITS[encoding] : undefined;
  if (
    !unit || !isInteger(offset) || offset < 1 || !isInteger(limit)
    || !isInteger(maxBytes) || maxBytes < 0 || maxBytes > MAX_PAYLOAD_BYTES
  ) {
    throw valueError(BAD_PAGE);
  }
  const fd = io(key, () => openSync(key, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK));
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
function write(args: Record<string, unknown>, { folder }: Context): null {
  const key = keyInFolder(folder, text(args, "key"));
  // Only the in-folder protected names are re-checked here. A key is proven to be inside the folder; a folder that
  // holds the home folder's credentials is refused at host start, and the system paths on the cloud's list are
  // left to the operating system's own permissions.
  if (protectedInFolder(folder, key)) throw sandboxError(inFolderRefusal(key));
  const encoded = text(args, "data");
  // Up to 50 MiB: a write's data that came in a transfer reaches the helper inline, once whole.
  if (encoded.length > Math.ceil(MAX_WRITE_BYTES / 3) * 4) throw WRITE_EFBIG;
  if (!isBase64(encoded)) throw valueError("data is not standard padded base64");
  const data = Buffer.from(encoded, "base64");
  if (data.length > MAX_WRITE_BYTES) throw WRITE_EFBIG;
  const parent = dirname(key);
  makeDirs(parent);
  // The rename replaces the name and never opens the file, so a file this user
  // cannot read is still replaced, as in the cloud.
  let existing: Stats | undefined;
  try {
    existing = lstatSync(key);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") io(key, () => { throw error; });
  }
  let mode: number | null = null;
  if (existing) {
    if (existing.isDirectory()) throw osError("EISDIR", key);
    if (!existing.isFile()) throw osError("EINVAL", key, "Not a regular file");
    if (existing.nlink > 1) throw osError("EMLINK", key, "File has more than one hard link, so it is not changed");
    mode = existing.mode & 0o7777;
  }
  const temporary = join(parent, `.surogate-${randomUUID()}.tmp`);
  const fd = io(key, () =>
    openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o666));
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
    io(key, () => renameSync(temporary, key));
  } catch (error) {
    try {
      unlinkSync(temporary);
    } catch {
      // Already gone.
    }
    throw error;
  }
  return null;
}

function remove(args: Record<string, unknown>, { folder }: Context): null {
  const key = keyInFolder(folder, text(args, "key"));
  if (protectedInFolder(folder, key)) throw sandboxError(inFolderRefusal(key));
  if (key === folder) throw osError("EISDIR", key);
  io(key, () => unlinkSync(key));
  return null;
}

// os.makedirs(dir, exist_ok=True): each missing parent first, so an error names
// the first folder that could not be made, as Python's does.
function makeDirs(dir: string): void {
  const parent = dirname(dir);
  if (parent !== dir && !existsSync(parent)) {
    try {
      makeDirs(parent);
    } catch (error) {
      if (!(error instanceof Failure && error.refusal.code === "EEXIST")) throw error;
    }
  }
  try {
    mkdirSync(dir);
  } catch (error) {
    let folderAlready = false;
    try {
      folderAlready = statSync(dir).isDirectory();
    } catch {
      // Not a folder.
    }
    if (!folderAlready) io(dir, () => { throw error; });
  }
}

function listDir(args: Record<string, unknown>, { folder }: Context): string[] {
  const key = keyInFolder(folder, text(args, "key"));
  return io(key, () => readdirSync(key)).slice(0, MAX_NAMES);
}

// shutil.which: a name with a slash is checked as it is (relative to *cwd*);
// otherwise the first PATH entry holding an executable file of that name, a
// relative or empty entry being read from *cwd*.
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

function which(args: Record<string, unknown>, { env, folder }: Context): boolean {
  return findOnPath(text(args, "name"), env.PATH, folder) !== null;
}

// The cloud's command line: --no-ignore, the key last, -e so a pattern may start with "-".
async function ripgrep(args: Record<string, unknown>, { env, folder }: Context, signal: AbortSignal): Promise<string> {
  const key = keyInFolder(folder, text(args, "key"));
  const mode = text(args, "mode");
  if (mode !== "files" && mode !== "count" && mode !== "json") throw valueError(`unknown search mode '${mode}'`);
  const pattern = text(args, "pattern");
  const glob = textOrNull(args, "glob");
  const lines = whole(args, "context");
  if (pattern.includes("\0") || glob?.includes("\0")) throw valueError("embedded null byte");
  const rg = findOnPath("rg", env.PATH, folder);
  if (!rg) throw new Failure({ type: "ripgrep", message: RG_MISSING });
  const argv = ["--no-ignore"];
  if (mode === "files") {
    argv.push("--files", "-g", pattern);
  } else {
    if (glob) argv.push("-g", glob);
    if (mode === "count") argv.push("-c");
    else argv.push("--json", ...(lines > 0 ? ["-C", String(lines)] : []));
    argv.push("-e", pattern);
  }
  argv.push(key);
  // A user's rg config could change the results or add --pre, which runs a program.
  const { RIPGREP_CONFIG_PATH: _config, ...clean } = env;
  return searchWith(rg, argv, clean, signal);
}

// ENOENT is no rg to run (or no interpreter for it); any other spawn error
// (E2BIG, EACCES, EMFILE) is an os error naming rg.
function spawnFailure(error: unknown, rg: string): unknown {
  return (error as NodeJS.ErrnoException | undefined)?.code === "ENOENT"
    ? new Failure({ type: "ripgrep", message: RG_MISSING })
    : fromNode(error, rg);
}

function searchWith(rg: string, argv: string[], env: Record<string, string | undefined>, signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(CANCELLED);
      return;
    }
    let child: ChildProcess;
    try {
      child = spawn(rg, argv, { env, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      reject(spawnFailure(error, rg));
      return;
    }
    // At once: an error event nobody listens for crashes the helper.
    child.on("error", (error) => {
      signal.removeEventListener("abort", onAbort);
      reject(spawnFailure(error, rg));
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    let size = 0;
    let errSize = 0;
    let over = false;
    const kill = () => {
      try {
        child.kill("SIGKILL");
      } catch {
        // Already gone.
      }
    };
    function onAbort() {
      kill();
      reject(CANCELLED);
    }
    signal.addEventListener("abort", onAbort, { once: true });
    // A child that could not start (EMFILE) has no streams.
    child.stdout?.on("data", (chunk: Buffer) => {
      size += chunk.length;
      // Every byte costs at least one character on the wire, so past the cap in
      // bytes the answer is already known.
      if (size > OUTPUT_CAP_CHARS) {
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
      signal.removeEventListener("abort", onAbort);
      if (signal.aborted) {
        reject(CANCELLED);
        return;
      }
      if (over) {
        reject(NARROW);
        return;
      }
      const status = code ?? -(killedBy ? osConstants.signals[killedBy] : 0);
      if (status !== 0 && status !== 1) {
        const stderr = [...UTF8.decode(Buffer.concat(err))].slice(0, 200).join("");
        reject(new Failure({ type: "ripgrep", message: `rg exited ${status}: ${stderr}` }));
        return;
      }
      const found = UTF8.decode(Buffer.concat(out));
      if (pyJsonLength(found) > OUTPUT_CAP_CHARS) reject(NARROW);
      else resolve(found);
    });
  });
}
