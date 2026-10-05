// Answers to file operations in the shapes surogates/devices/workspace.py names,
// worded as the cloud's Python words them: an OSError's code and the text its
// str() shows, so a handler and the model read the same on a laptop as in the cloud.

import { getSystemErrorMessage } from "node:util";

export const MAX_PAYLOAD_BYTES = 1024 * 1024;
export const MAX_READ_BYTES = 50 * 1024 * 1024;
export const MAX_WRITE_BYTES = 50 * 1024 * 1024;
export const MAX_MESSAGE_CHARS = 1536 * 1024;
export const OUTPUT_CAP_CHARS = 256 * 1024;
export const MAX_NAMES = 10_000;
export const READ_TOO_LARGE = "File too large to read from a local folder (over 50 MiB)";
export const WRITE_TOO_LARGE = "File too large to write to a local folder (over 50 MiB)";

export type Refusal = { type: string; message: string; [detail: string]: unknown };

// An operation that cannot be done, carrying the error it is answered with.
export class Failure extends Error {
  constructor(readonly refusal: Refusal) {
    super(refusal.message);
  }
}

// glibc's texts, which Python shows; libuv's differ for some ("illegal operation
// on a directory").
const STRERROR: Record<string, string> = {
  ENOENT: "No such file or directory",
  EACCES: "Permission denied",
  EISDIR: "Is a directory",
  ENOTDIR: "Not a directory",
  EEXIST: "File exists",
  EPERM: "Operation not permitted",
  EROFS: "Read-only file system",
  ELOOP: "Too many levels of symbolic links",
  EMLINK: "Too many links",
  EFBIG: "File too large",
  ENOTEMPTY: "Directory not empty",
  ENXIO: "No such device or address",
  EINVAL: "Invalid argument",
  EXDEV: "Invalid cross-device link",
  EBUSY: "Device or resource busy",
  ENAMETOOLONG: "File name too long",
  ENOSPC: "No space left on device",
  EIO: "Input/output error",
  EBADF: "Bad file descriptor",
  EMFILE: "Too many open files",
  ENFILE: "Too many open files in system",
  EDQUOT: "Disk quota exceeded",
  ETXTBSY: "Text file busy",
  ENODEV: "No such device",
  ESTALE: "Stale file handle",
  ENOTSUP: "Operation not supported",
  EOPNOTSUPP: "Operation not supported",
  ESPIPE: "Illegal seek",
  ENOMEM: "Cannot allocate memory",
  EFAULT: "Bad address",
  EILSEQ: "Invalid or incomplete multibyte or wide character",
  ERANGE: "Numerical result out of range",
  ENOTCONN: "Transport endpoint is not connected",
  EHOSTUNREACH: "No route to host",
  ENOLCK: "No locks available",
  EUCLEAN: "Structure needs cleaning",
  ENOMEDIUM: "No medium found",
};

// Python's repr() of a str: the quote it picks, its escapes, and \x, \u or \U
// for what str.isprintable() rejects.
export function pyRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of text) {
    const point = ch.codePointAt(0) ?? 0;
    if (ch === quote || ch === "\\") out += `\\${ch}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (point === 0x20 || /^[\p{L}\p{M}\p{N}\p{P}\p{S}]$/u.test(ch)) out += ch;
    else if (point < 0x100) out += `\\x${point.toString(16).padStart(2, "0")}`;
    else if (point < 0x10000) out += `\\u${point.toString(16).padStart(4, "0")}`;
    else out += `\\U${point.toString(16).padStart(8, "0")}`;
  }
  return out + quote;
}

// len(json.dumps(text)) in Python: ensure_ascii escapes every non-ASCII UTF-16
// unit to six characters, which no JSON encoder exceeds.
export function pyJsonLength(text: string): number {
  let length = 2;
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i);
    if (unit === 0x22 || unit === 0x5c || unit === 0x08 || unit === 0x0c || unit === 0x0a || unit === 0x0d || unit === 0x09) {
      length += 2;
    } else if (unit >= 0x20 && unit <= 0x7e) {
      length += 1;
    } else {
      length += 6;
    }
  }
  return length;
}

// OSError(code, text, path): "<text>: <repr(path)>", without the "[Errno N] ".
export function osError(code: string, path: string, text?: string): Failure {
  return new Failure({ type: "os", code, message: `${text ?? STRERROR[code] ?? code}: ${pyRepr(path)}` });
}

export function valueError(message: string): Failure {
  return new Failure({ type: "value", message });
}

export function sandboxError(message: string): Failure {
  return new Failure({ type: "sandbox", message });
}

// A write whose file is not at the revision it expects: the model reads it again.
export function conflict(key: string): Failure {
  return new Failure({
    type: "conflict",
    message: `${key} changed on this computer after it was read, so it was not written. Read it again, then make the change again`,
  });
}

// A filesystem call about *path*: a Node error comes out as Python's OSError.
// For synchronous calls only: a promise or a callback would escape the try.
export function io<T>(path: string, call: () => T): T {
  try {
    return call();
  } catch (error) {
    throw fromNode(error, path);
  }
}

export function fromNode(error: unknown, path: string): unknown {
  if (!(error instanceof Error) || error instanceof Failure) return error;
  const { code, errno } = error as NodeJS.ErrnoException;
  if (typeof code !== "string" || typeof errno !== "number") return error;
  const libuv = getSystemErrorMessage(errno);
  return osError(code, path, STRERROR[code] ?? libuv.charAt(0).toUpperCase() + libuv.slice(1));
}
