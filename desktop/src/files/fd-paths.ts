// What the file host does through a folder it holds open: a file in it is reached through the folder's descriptor,
// never by the folder's path again, so nothing put on that path since is followed (operations.ts, walk; land.ts;
// hosts/hooks.ts). Each OS names a file through a descriptor its own way: Linux by the descriptor's entry in
// /proc/self/fd (fd-paths-linux.ts), macOS by Darwin's *at calls on the descriptor (fd-paths-darwin.ts).

import type { BigIntStats, Dirent, Stats } from "node:fs";

import { darwinFdPaths } from "./fd-paths-darwin.js";
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

// On a system the file host is not built for yet, every call is refused, in notBuilt's words (platform.ts): files/ is on
// the agent's disk, which holds no platform.js, so it keeps its own copy of the OSes' names. The object itself is shown
// as any other is.
export const OS_NAMES: Readonly<Partial<Record<NodeJS.Platform, string>>> = { darwin: "macOS", win32: "Windows" };
const NOT_BUILT = `The file host is not built for ${OS_NAMES[process.platform] ?? process.platform} yet`;
const refused = (): never => {
  throw new Error(NOT_BUILT);
};
const unbuilt: FdPaths = {
  hold: refused, open: refused, lstat: refused, stat: refused, access: refused, mkdir: refused, rmdir: refused, unlink: refused, chmod: refused,
  link: refused, rename: refused, copy: refused, listing: refused, pathOf: refused, handed: refused, shown: refused,
};

export const fdPaths: FdPaths = ({ linux: linuxFdPaths, darwin: darwinFdPaths } as Partial<Record<NodeJS.Platform, FdPaths>>)[process.platform] ?? unbuilt;
