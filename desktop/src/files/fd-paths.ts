// What the file host does through a folder it holds open: a file in it is reached through the folder's descriptor,
// never by the folder's path again, so nothing put on that path since is followed (operations.ts, walk; land.ts;
// hosts/hooks.ts). Each OS names a file through a descriptor its own way: Linux by the descriptor's entry in
// /proc/self/fd (fd-paths-linux.ts), macOS by Darwin's *at calls on the descriptor (fd-paths-darwin.ts), Windows by
// ntat's opens relative to each folder's handle (fd-paths-win32.ts). On Linux and macOS a path is reached by Node's own
// call on it, inside the sandbox that binds the folder. Windows has no sandbox: there a path is one in a folder this
// process bound, walked from that folder's handle, and the file helper reaches no file of a folder but through here.

import type { BigIntStats, Dirent, Stats } from "node:fs";

import { darwinFdPaths } from "./fd-paths-darwin.js";
import { linuxFdPaths } from "./fd-paths-linux.js";
import { win32FdPaths } from "./fd-paths-win32.js";
import { notBuiltWords } from "./os-names.js";

/** A file by its path, or by its *name* in the folder held open as *dir*: with no name, that folder itself. */
export type Where = string | { readonly dir: number; readonly name?: string };

/** A folder's entries, read one at a time, each name as its bytes. */
export interface Listing {
  readSync(): Dirent<Buffer> | null;
  closeSync(): void;
}

/** A path as a program this process starts is given it, and what holds it there while the program runs. */
export interface Outside {
  path: string;
  /** Whether the folder at the path held nothing as it was held, so that a program would find nothing in it. */
  empty: boolean;
  release(): void;
}

/** Node's calls of the same names, each on a Where; each throws as Node's does. */
export interface FdPaths {
  /**
   * The folders this process works in, by their paths. Windows holds each open from here on, and reaches a path only in
   * one of them, from its handle; a path in none is not there. On Linux and macOS the sandbox binds them, and this does
   * nothing.
   */
  bind(folders: readonly string[]): void;
  /** The folder at *where*, held to be passed through, and never through a link at its name. */
  hold(where: Where): number;
  open(where: Where, flags: number, mode?: number): number;
  lstat(where: Where): BigIntStats;
  stat(where: Where): Stats;
  stat(where: Where, options: { bigint: true }): BigIntStats;
  /** Whether stat finds anything at *where*. */
  exists(where: Where): boolean;
  /** The names in the folder at *where*. */
  readdir(where: Where): string[];
  access(where: Where, mode: number): void;
  mkdir(where: Where): void;
  rmdir(where: Where): void;
  unlink(where: Where): void;
  chmod(where: Where, mode: number): void;
  link(from: Where, to: Where): void;
  rename(from: Where, to: Where): void;
  copy(from: Where, to: Where): void;
  /** What the link at *path* holds, as UTF-8. */
  readlink(path: string): string;
  /** The entries of the folder held as *dir*. */
  listing(dir: number): Listing;
  /** Where the file open as *fd* is now: null for one removed since. */
  pathOf(fd: number): string | null;
  /** Each descriptor above the standard three that this process holds without close-on-exec: a child of its would inherit it. */
  handed(): number[];
  /** *where* as a failure names it. */
  shown(where: Where): string;
  /**
   * The path *path* has to a program this process starts, which opens it by its path: on Linux and macOS the path itself,
   * in the sandbox that binds it; on Windows this computer's path for it, each folder from the bound one down to it held
   * so that none of them can be renamed, removed or made a link until it is released.
   */
  outside(path: string): Outside;
}

// On a system the file host is not built for yet, every call is refused, in notBuilt's words (platform.ts, os-names.ts).
// The object itself is shown as any other is.
const NOT_BUILT = notBuiltWords("The file host");
const refused = (): never => {
  throw new Error(NOT_BUILT);
};
const unbuilt: FdPaths = {
  bind: refused, hold: refused, open: refused, lstat: refused, stat: refused, exists: refused, readdir: refused, access: refused, mkdir: refused,
  rmdir: refused, unlink: refused, chmod: refused, link: refused, rename: refused, copy: refused, readlink: refused, listing: refused,
  pathOf: refused, handed: refused, shown: refused, outside: refused,
};

export const fdPaths: FdPaths = ({ linux: linuxFdPaths, darwin: darwinFdPaths, win32: win32FdPaths } as Partial<Record<NodeJS.Platform, FdPaths>>)[
  process.platform
] ?? unbuilt;
