// A file's identity, a folder's above all: its device and its inode, as every caller compares them and every record
// keeps them (a binding, a place, a landing). Each OS gives them its own way; on Linux and macOS they are fs's own, on
// Windows the walk's.
// files/ is on the agent disk, which holds no platform.js: the OS is read here, and said as notBuilt says it.

import type { BigIntStats, Stats } from "node:fs";

import { notBuiltWords } from "./os-names.js";

/** A file's device and inode, as numbers or as bigints, as fs gave them. */
export interface Identity<N extends number | bigint = number> {
  dev: N;
  ino: N;
}

const own = (stats: Stats | BigIntStats): Identity<number | bigint> => ({ dev: stats.dev, ino: stats.ino });

// Windows's: Node's ino there is a double, which NTFS's 64-bit file references overflow, and ReFS's 128-bit IDs are wider
// still. So a file's identity is what the walk (fd-paths-win32.ts) reads of its handle, FILE_ID_INFO, made two numbers
// as every caller and record keeps them: dev the volume serial's low 32 bits, ino the first 52 bits of SHA-256 over the
// serial and the ID. Two files meet by chance once in 2^52, and an ID is not an attacker's to choose. The walk's stats
// carry them; fs's own carry Node's, and are refused.
const WALKED = new WeakSet<object>();
/** *stats*, made by the walk on Windows, as stats whose dev and ino are the file's identity. */
export function walked<S extends Stats | BigIntStats>(stats: S): S {
  WALKED.add(stats);
  return stats;
}
const WALK_ONLY = "A file's identity on Windows is read through the file host's walk, not from Node's stats";
const fromTheWalk = (stats: Stats | BigIntStats): Identity<number | bigint> => {
  if (!WALKED.has(stats)) throw new Error(WALK_ONLY);
  return own(stats);
};
const ROWS: Partial<Record<NodeJS.Platform, typeof own>> = { linux: own, darwin: own, win32: fromTheWalk };
// On an OS whose identity is not built yet, in notBuilt's words (os-names.ts).
const NOT_BUILT = notBuiltWords("A folder's identity");
const unbuilt = (): never => {
  throw new Error(NOT_BUILT);
};
const read = ROWS[process.platform] ?? unbuilt;

/** The identity of the file *stats* are of: Node's numbers, or its bigints for BigIntStats. */
export function identityOf(stats: Stats): Identity;
export function identityOf(stats: BigIntStats): Identity<bigint>;
export function identityOf(stats: Stats | BigIntStats): Identity<number | bigint>;
export function identityOf(stats: Stats | BigIntStats): Identity<number | bigint> {
  return read(stats);
}

/** Whether *a* and *b* are of one file, by its identity. */
export function sameFile(a: Stats | BigIntStats, b: Stats | BigIntStats): boolean {
  const [one, other] = [identityOf(a), identityOf(b)];
  return one.dev === other.dev && one.ino === other.ino;
}
