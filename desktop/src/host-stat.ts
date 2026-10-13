// A file's stats by this computer's own path, its identity among them (files/identity.ts), for what the app and its
// hosts look at outside the file host's walk: a chat's folder as it is bound and checked, the app's data, a PATH's
// folders. Node's own on Linux and macOS; on Windows read through ntat (files/fd-paths-win32.ts, statsAt), as Node's
// numbers there do not hold a file's ID. Electron-free.

import { lstatSync, type Stats, statSync } from "node:fs";

import { statsAt } from "./files/fd-paths-win32.js";
import { forThisOs, notBuilt } from "./platform.js";

interface Rows {
  stat(path: string): Stats;
  lstat(path: string): Stats;
}

const NODE: Rows = { stat: (path) => statSync(path), lstat: (path) => lstatSync(path) };
const ROWS = forThisOs<Rows>({ linux: NODE, darwin: NODE, win32: { stat: (path) => statsAt(path, false), lstat: (path) => statsAt(path, true) } });

/** What is at *path*, through its links, as statSync gives it. */
export const hostStat = (path: string): Stats => (ROWS ?? notBuilt("A file's identity")).stat(path);

/** What is at *path*, a link there itself, as lstatSync gives it. */
export const hostLstat = (path: string): Stats => (ROWS ?? notBuilt("A file's identity")).lstat(path);
