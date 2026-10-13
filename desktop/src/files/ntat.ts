// ntat, the file host's calls relative to a folder it holds open, on Windows (native/ntat/ntat.c): each name opened
// relative to the handle of the folder before it, never through a reparse point, every name checked before it is
// opened and after. Loaded on its first use, and only on Windows: in the file host and its helper, which run on the
// app's own node. It lies in dist/native/, beside the app's code. Each failure is thrown as Node's own are, its code
// the errno's name libuv gives the Win32 error, with the NT status or Win32 error kept beside it, and with why ntat
// refused it where ntat did.

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { getSystemErrorMessage, getSystemErrorName } from "node:util";

/** Windows's numbers for what ntat takes and gives, as its headers define them. */
export interface NtatConstants {
  readonly FILE_READ_DATA: number;
  readonly FILE_LIST_DIRECTORY: number;
  readonly FILE_WRITE_DATA: number;
  readonly FILE_APPEND_DATA: number;
  readonly FILE_TRAVERSE: number;
  readonly FILE_READ_ATTRIBUTES: number;
  readonly FILE_WRITE_ATTRIBUTES: number;
  readonly FILE_GENERIC_READ: number;
  readonly FILE_GENERIC_WRITE: number;
  readonly DELETE: number;
  readonly SYNCHRONIZE: number;
  readonly FILE_SHARE_READ: number;
  readonly FILE_SHARE_WRITE: number;
  readonly FILE_SHARE_DELETE: number;
  readonly FILE_OPEN: number;
  readonly FILE_CREATE: number;
  readonly FILE_OPEN_IF: number;
  readonly FILE_OVERWRITE: number;
  readonly FILE_OVERWRITE_IF: number;
  readonly FILE_DIRECTORY_FILE: number;
  readonly FILE_NON_DIRECTORY_FILE: number;
  readonly FILE_ATTRIBUTE_READONLY: number;
  readonly FILE_ATTRIBUTE_DIRECTORY: number;
  readonly FILE_ATTRIBUTE_NORMAL: number;
  readonly FILE_ATTRIBUTE_REPARSE_POINT: number;
  readonly IO_REPARSE_TAG_MOUNT_POINT: number;
  readonly IO_REPARSE_TAG_SYMLINK: number;
  readonly IO_REPARSE_TAG_CLOUD: number;
  readonly IO_REPARSE_TAG_CLOUD_MASK: number;
}

/** A file's identity on Windows: its volume's 64-bit serial and its 128-bit file ID (FILE_ID_INFO). */
export interface FileId {
  volume: bigint;
  id: Buffer;
}

/** What a handle is, as Windows says it: each time in 100 ns since 1601. */
export interface NtatQuery extends FileId {
  attributes: number;
  /** The reparse tag of a reparse point; null for a file or folder that is none. */
  tag: number | null;
  links: number;
  size: bigint;
  allocated: bigint;
  directory: boolean;
  deletePending: boolean;
  created: bigint;
  accessed: bigint;
  written: bigint;
  changed: bigint;
  /** The name it is open by, every short name expanded: its normalized path's last component. */
  longName: string | null;
  /** Its 8.3 name, where its volume made one. */
  shortName: string | null;
  /** For a folder, whether it compares its names by case; null where its volume does not say. */
  caseSensitive: boolean | null;
}

/** One entry of a folder: its name's bytes (a name that is no UTF-16 text is no UTF-8 either), its attributes, and its reparse tag or 0. */
export type NtatEntry = [name: Buffer, attributes: number, tag: number];

/**
 * Every descriptor ntat takes or gives is one of node's own (uv_open_osfhandle), closed by Node's closeSync. A name is
 * one name, checked as Windows names are (names-win32.ts); anything else is refused before any call.
 */
export interface Ntat {
  constants: NtatConstants;
  /** The bound folder by its whole path, \\?\C:\..., refused where it is a reparse point or no folder. */
  openRoot(path: string): number;
  openAt(dir: number, name: string, access: number, disposition: number, options: number, share: number, attributes: number, links: boolean): number;
  /** The folder open as *dir* again, with a handle of its own. */
  reopen(dir: number, access: number, share: number): number;
  mkdirAt(dir: number, name: string): void;
  unlinkAt(dir: number, name: string, folder: boolean): void;
  renameAt(fromDir: number, from: string, toDir: number, to: string, replace: boolean): void;
  linkAt(fromDir: number, from: string, toDir: number, to: string): void;
  query(fd: number): NtatQuery;
  /** The next entries of the folder open as *dir*, a listing's own descriptor; from its first with *restart*; null at its end. */
  list(dir: number, restart: boolean): NtatEntry[] | null;
  /** \\?\C:\..., as Windows names the file open as *fd* now. */
  pathOf(fd: number): string;
  /** Whether a child this process starts would be given the handle under *fd*; null for one not open. */
  inheritable(fd: number): boolean | null;
  /** How many handles this process holds. */
  handles(): number;
  /** This boot's number. */
  bootId(): number;
  /** This process's user's SID. */
  userSid(): string;
}

// The same from src/files and from dist/files.
export const NTAT = fileURLToPath(new URL("../../dist/native/ntat.node", import.meta.url));

/** Why ntat refused a call itself: a name Windows aliases, a reparse point on the way, or a name that opened as another. */
export type Refused = "name" | "link" | "alias";

/** A failure as Node says one, with what ntat said beside it. */
export type NtatError = NodeJS.ErrnoException & { winerror?: number; ntstatus?: number; refused?: Refused };

function nodeError(error: unknown, path: string | undefined): unknown {
  const { errno, syscall, winerror, ntstatus, refused } = error as { errno?: unknown; syscall?: unknown; winerror?: unknown; ntstatus?: unknown; refused?: unknown };
  if (typeof errno !== "number" || typeof syscall !== "string") return error;
  const code = getSystemErrorName(errno);
  const at = path === undefined ? "" : ` '${path}'`;
  return Object.assign(new Error(`${code}: ${getSystemErrorMessage(errno)}, ${syscall}${at}`), {
    errno, code, syscall, ...(path === undefined ? {} : { path }), ...(typeof winerror === "number" ? { winerror } : {}),
    ...(typeof ntstatus === "number" ? { ntstatus } : {}), ...(typeof refused === "string" ? { refused } : {}),
  });
}

let loaded: Ntat | null = null;

/** ntat, loaded on its first use. */
export function ntat(): Ntat {
  if (loaded) return loaded;
  const raw = createRequire(import.meta.url)(NTAT) as Ntat;
  // Each call by the name its failure names: its first name argument.
  const named = <A extends unknown[], R>(call: (...args: A) => R, at: (args: A) => string | undefined) => (...args: A): R => {
    try {
      return call(...args);
    } catch (error) {
      throw nodeError(error, at(args));
    }
  };
  loaded = {
    constants: raw.constants,
    openRoot: named(raw.openRoot, ([path]) => path),
    openAt: named(raw.openAt, ([, name]) => name),
    reopen: named(raw.reopen, () => undefined),
    mkdirAt: named(raw.mkdirAt, ([, name]) => name),
    unlinkAt: named(raw.unlinkAt, ([, name]) => name),
    renameAt: named(raw.renameAt, ([, from]) => from),
    linkAt: named(raw.linkAt, ([, from]) => from),
    query: named(raw.query, () => undefined),
    list: named(raw.list, () => undefined),
    pathOf: named(raw.pathOf, () => undefined),
    inheritable: named(raw.inheritable, () => undefined),
    handles: named(raw.handles, () => undefined),
    bootId: named(raw.bootId, () => undefined),
    userSid: named(raw.userSid, () => undefined),
  };
  return loaded;
}
