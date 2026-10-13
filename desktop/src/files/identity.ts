// A file's identity, a folder's above all: its device and its inode, as every caller compares them and every record
// keeps them (a binding, a place, a landing). Each OS gives them its own way; on Linux and macOS they are fs's own.
// files/ is on the agent disk, which holds no platform.js: the OS is read here, and said as notBuilt says it.

import type { BigIntStats, Stats } from "node:fs";

/** A file's device and inode, as numbers or as bigints, as fs gave them. */
export interface Identity<N extends number | bigint = number> {
  dev: N;
  ino: N;
}

const own = (stats: Stats | BigIntStats): Identity<number | bigint> => ({ dev: stats.dev, ino: stats.ino });
const ROWS: Partial<Record<NodeJS.Platform, typeof own>> = { linux: own, darwin: own };
// On an OS whose identity is not built yet, in notBuilt's words (platform.ts).
const NAMES: Partial<Record<NodeJS.Platform, string>> = { darwin: "macOS", win32: "Windows" };
const NOT_BUILT = `A folder's identity is not built for ${NAMES[process.platform] ?? process.platform} yet`;
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
