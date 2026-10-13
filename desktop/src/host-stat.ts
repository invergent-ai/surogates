// A file's stats by this computer's own path, its identity among them (files/identity.ts), for what the app and its
// hosts look at outside the file host's walk: a chat's folder as it is bound and checked, the app's data, a PATH's
// folders. Node's own on Linux and macOS; on Windows read through ntat (files/fd-paths-win32.ts, statsAt), as Node's
// numbers there do not hold a file's ID. Electron-free.

import { lstatSync, type Stats, statSync } from "node:fs";
import { lstat } from "node:fs/promises";

import { statsAt } from "./files/fd-paths-win32.js";
import { forThisOs, notBuilt } from "./platform.js";

interface Rows {
  stat(path: string): Stats;
  lstat(path: string): Stats;
  looked(path: string): Promise<Stats>;
}

const NODE: Rows = { stat: (path) => statSync(path), lstat: (path) => lstatSync(path), looked: (path) => lstat(path) };
const ROWS = forThisOs<Rows>({
  linux: NODE,
  darwin: NODE,
  // ntat's calls are synchronous: Windows's file host works in no folder on a network volume (binding/folder.ts), the one
  // kind whose look would not answer.
  win32: { stat: (path) => statsAt(path, false), lstat: (path) => statsAt(path, true), looked: (path) => new Promise((resolve) => resolve(statsAt(path, true))) },
});

/** What is at *path*, through its links, as statSync gives it. */
export const hostStat = (path: string): Stats => (ROWS ?? notBuilt("A file's identity")).stat(path);

/** What is at *path*, a link there itself, as lstatSync gives it. */
export const hostLstat = (path: string): Stats => (ROWS ?? notBuilt("A file's identity")).lstat(path);

/** What is at *path*, a link there itself, as fs.promises' lstat gives it, on libuv's threads where this OS can. */
export const hostLooked = (path: string): Promise<Stats> => (ROWS ?? notBuilt("A file's identity")).looked(path);
