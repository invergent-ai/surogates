// fsat, the file host's calls relative to a folder it holds open, on macOS (native/fsat/fsat.c): Darwin's *at
// calls, which Node does not expose. Loaded on its first use, and only on macOS: in the file host and its helper,
// which run on the app's own node, never in Electron's processes. It lies in dist/native/, beside the app's code.
// Each failure is thrown as Node's own are, its code the errno's name.

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { getSystemErrorMessage, getSystemErrorName } from "node:util";

/** Darwin's numbers for the flags and kinds fsat takes and gives, as its headers define them. */
export interface FsatConstants {
  readonly O_RDONLY: number;
  readonly O_WRONLY: number;
  readonly O_RDWR: number;
  readonly O_CREAT: number;
  readonly O_EXCL: number;
  readonly O_TRUNC: number;
  readonly O_NONBLOCK: number;
  readonly O_DIRECTORY: number;
  readonly O_NOFOLLOW: number;
  readonly O_NOFOLLOW_ANY: number;
  readonly O_CLOEXEC: number;
  readonly O_EXLOCK: number;
  readonly O_SYMLINK: number;
  readonly AT_FDCWD: number;
  readonly AT_SYMLINK_NOFOLLOW: number;
  readonly AT_REMOVEDIR: number;
  readonly RENAME_EXCL: number;
  readonly FD_CLOEXEC: number;
  readonly DT_DIR: number;
  readonly DT_REG: number;
  readonly DT_LNK: number;
  readonly DT_FIFO: number;
  readonly DT_SOCK: number;
  readonly DT_CHR: number;
  readonly DT_BLK: number;
  /** Where the SDK defines it. */
  readonly O_SEARCH?: number;
}

/** A folder's entries as fsat reads them: its own, opaque. */
export type FsatListing = { readonly __fsatListing: unique symbol };

/** A stat as libuv gives Node one, each time as its seconds and nanoseconds. */
export interface FsatStats {
  dev: bigint;
  ino: bigint;
  mode: bigint;
  nlink: bigint;
  uid: bigint;
  gid: bigint;
  rdev: bigint;
  size: bigint;
  blksize: bigint;
  blocks: bigint;
  atimeSec: number;
  atimeNsec: number;
  mtimeSec: number;
  mtimeNsec: number;
  ctimeSec: number;
  ctimeNsec: number;
  birthtimeSec: number;
  birthtimeNsec: number;
}

/**
 * A name is one name in the folder held as *dir*, or "." for that folder itself, never "" or "..", and holds no "/";
 * with AT_FDCWD for *dir*, an absolute path instead. Anything else is refused before any call, with EINVAL. A null
 * name is the descriptor *dir* itself, whatever it is open on.
 */
export interface Fsat {
  constants: FsatConstants;
  openat(dir: number, name: string, flags: number, mode: number): number;
  fstatat(dir: number, name: string | null, flags: number): FsatStats;
  faccessat(dir: number, name: string, mode: number, flags: number): void;
  fchmodat(dir: number, name: string | null, mode: number, flags: number): void;
  mkdirat(dir: number, name: string, mode: number): void;
  unlinkat(dir: number, name: string, flags: number): void;
  renameatx(fromDir: number, from: string, toDir: number, to: string, flags: number): void;
  linkat(fromDir: number, from: string, toDir: number, to: string, flags: number): void;
  /** A listing of the folder held as *dir*, on a descriptor of its own, from its first entry. */
  opendir(dir: number): FsatListing;
  /** The listing's next entry but "." and "..", its name's bytes and its d_type; null at its end. */
  readdir(listing: FsatListing): [name: Buffer, type: number] | null;
  closedir(listing: FsatListing): void;
  /** fcntl(F_GETPATH): the path the file open as *fd* has now. */
  getpath(fd: number): string;
  /** Whether *fd* is close-on-exec; null for one that is not open. */
  cloexec(fd: number): boolean | null;
}

// The same from src/files and from dist/files.
export const FSAT = fileURLToPath(new URL("../../dist/native/fsat.node", import.meta.url));

// A failure as Node says one: "ENOENT: no such file or directory, openat 'name'", with its errno, code, syscall and path.
function nodeError(error: unknown, path: string | undefined): unknown {
  const { errno, syscall } = error as { errno?: unknown; syscall?: unknown };
  if (typeof errno !== "number" || typeof syscall !== "string") return error;
  const code = getSystemErrorName(errno);
  const at = path === undefined ? "" : ` '${path}'`;
  return Object.assign(new Error(`${code}: ${getSystemErrorMessage(errno)}, ${syscall}${at}`), { errno, code, syscall, ...(path === undefined ? {} : { path }) });
}

let loaded: Fsat | null = null;

/** fsat, loaded on its first use. */
export function fsat(): Fsat {
  if (loaded) return loaded;
  const raw = createRequire(import.meta.url)(FSAT) as Fsat;
  // Each call by the name its failure names: its first name argument.
  const named = <A extends unknown[], R>(call: (...args: A) => R, at: (args: A) => string | null | undefined) => (...args: A): R => {
    try {
      return call(...args);
    } catch (error) {
      throw nodeError(error, at(args) ?? undefined);
    }
  };
  loaded = {
    constants: raw.constants,
    openat: named(raw.openat, ([, name]) => name),
    fstatat: named(raw.fstatat, ([, name]) => name),
    faccessat: named(raw.faccessat, ([, name]) => name),
    fchmodat: named(raw.fchmodat, ([, name]) => name),
    mkdirat: named(raw.mkdirat, ([, name]) => name),
    unlinkat: named(raw.unlinkat, ([, name]) => name),
    renameatx: named(raw.renameatx, ([, from]) => from),
    linkat: named(raw.linkat, ([, from]) => from),
    opendir: named(raw.opendir, () => undefined),
    readdir: named(raw.readdir, () => undefined),
    closedir: named(raw.closedir, () => undefined),
    getpath: named(raw.getpath, () => undefined),
    cloexec: named(raw.cloexec, () => undefined),
  };
  return loaded;
}
