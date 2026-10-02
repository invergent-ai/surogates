// The file operations, as surogates/devices/workspace.py defines them and the
// cloud's LocalWorkspaceIO does them, run by the file helper inside the folder's
// sandbox. Each is synchronous fs work, so two changes to one path never
// interleave; only ripgrep waits on a process.

import { randomUUID } from "node:crypto";
import {
  closeSync, constants, existsSync, fchmodSync, fstatSync, lstatSync, mkdirSync, openSync, readdirSync, readSync,
  renameSync, type Stats, statSync, unlinkSync, writeSync,
} from "node:fs";
import { dirname, join } from "node:path";

import type { Outcome } from "../link/protocol.js";
import {
  Failure, io, MAX_MESSAGE_CHARS, MAX_NAMES, MAX_PAYLOAD_BYTES, osError, sandboxError, TOO_LARGE, valueError,
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
  write,
  delete: remove,
  list_dir: listDir,
};

const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const EFBIG = new Failure({ type: "os", code: "EFBIG", message: TOO_LARGE });

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
  if (JSON.stringify(outcome).length > MAX_MESSAGE_CHARS) {
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
    return { is_dir: st.isDirectory(), size: Number(st.size), mtime: Number(seconds) + Number(nanoseconds) * 1e-9 };
  } catch {
    return null;
  }
}

// An open file that is a regular file; refused otherwise, before any byte moves.
function regular(fd: number, key: string): void {
  const st = fstatSync(fd);
  if (st.isDirectory()) throw osError("EISDIR", key);
  if (!st.isFile()) throw osError("EINVAL", key, "Not a regular file");
}

function read(args: Record<string, unknown>, { folder }: Context): string {
  const key = keyInFolder(folder, text(args, "key"));
  const wanted = wholeOrNull(args, "max_bytes");
  const limit = wanted === null ? MAX_PAYLOAD_BYTES + 1 : Math.min(wanted, MAX_PAYLOAD_BYTES + 1);
  const fd = io(key, () => openSync(key, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK));
  try {
    regular(fd, key);
    const buffer = Buffer.alloc(limit);
    let size = 0;
    while (size < limit) {
      const count = io(key, () => readSync(fd, buffer, size, limit - size, null));
      if (count === 0) break;
      size += count;
    }
    if (size > MAX_PAYLOAD_BYTES) throw EFBIG;
    return buffer.subarray(0, size).toString("base64");
  } finally {
    io(key, () => closeSync(fd));
  }
}

// Temp file and atomic rename, keeping the replaced file's mode, as the cloud does.
function write(args: Record<string, unknown>, { folder }: Context): null {
  const key = keyInFolder(folder, text(args, "key"));
  if (protectedInFolder(folder, key)) throw sandboxError(inFolderRefusal(key));
  const encoded = text(args, "data");
  // The check comes first: the pattern overflows the regex engine's stack on megabytes of text.
  if (encoded.length > Math.ceil(MAX_PAYLOAD_BYTES / 3) * 4) throw EFBIG;
  if (!BASE64.test(encoded)) throw valueError("data is not standard padded base64");
  const data = Buffer.from(encoded, "base64");
  if (data.length > MAX_PAYLOAD_BYTES) throw EFBIG;
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
