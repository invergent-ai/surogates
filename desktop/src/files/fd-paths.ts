// What the file host does through a folder it holds open: a file in it is reached through the folder's descriptor,
// never by the folder's path again, so nothing put on that path since is followed (operations.ts, walk; land.ts;
// hosts/hooks.ts). Each OS names a file through a descriptor its own way: Linux by the descriptor's entry in
// /proc/self/fd (fd-paths-linux.ts).

import type { BigIntStats, Dirent, Stats } from "node:fs";

import { linuxFdPaths } from "./fd-paths-linux.js";

/** A file by its path, or by its *name* in the folder held open as *dir*: with no name, that folder itself. */
export type Where = string | { readonly dir: number; readonly name?: string };

/** A folder's entries, read one at a time, each name as its bytes. */
export interface Listing {
  readSync(): Dirent<Buffer> | null;
  closeSync(): void;
}

/** Node's calls of the same names, each on a Where; each throws as Node's does. */
export interface FdPaths {
  /** The folder at *where*, held to be passed through, and never through a link at its name. */
  hold(where: Where): number;
  open(where: Where, flags: number, mode?: number): number;
  lstat(where: Where): BigIntStats;
  stat(where: Where): Stats;
  access(where: Where, mode: number): void;
  mkdir(where: Where): void;
  rmdir(where: Where): void;
  unlink(where: Where): void;
  chmod(where: Where, mode: number): void;
  link(from: Where, to: Where): void;
  rename(from: Where, to: Where): void;
  copy(from: Where, to: Where): void;
  /** The entries of the folder held as *dir*. */
  listing(dir: number): Listing;
  /** Where the file open as *fd* is now: null for one removed since. */
  pathOf(fd: number): string | null;
  /** Each descriptor above the standard three that this process holds without close-on-exec: a child of its would inherit it. */
  handed(): number[];
  /** *where* as a failure names it. */
  shown(where: Where): string;
}

// On a system the file host is not built for yet, every call is refused.
const NOT_BUILT = "The file tools are not built for this computer's system yet";
const unbuilt = new Proxy({} as FdPaths, {
  get: () => () => {
    throw new Error(NOT_BUILT);
  },
});

export const fdPaths: FdPaths = process.platform === "linux" ? linuxFdPaths : unbuilt;
