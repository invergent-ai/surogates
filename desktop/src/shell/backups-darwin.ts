// The VM's disks kept out of Time Machine: a 32 GiB sessions disk and a guest image of some gigabytes,
// which every hourly backup would copy again. The VM's folder gets a sticky exclusion, which tmutil(8)
// makes for an item without an administrator ("addexclusion ... the sticky exclusion ... follows a file
// or directory when it is moved"; Apple, tmutil(8)), and what it holds is left out with it. It is asked
// once: a mark in the folder says it was made, and goes with the folder.

import { execFile } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { cleanly } from "../clean-child.js";

const TMUTIL = "/usr/bin/tmutil";
export const MARK = ".kept-out-of-time-machine";

// A run of tmutil with *args*, as every program the app starts, with none of the user's environment.
function tmutil(args: string[]): Promise<void> {
  return new Promise((resolve, reject) => {
    execFile(...cleanly(TMUTIL, args), { env: { PATH: "/usr/bin:/bin" }, timeout: 30_000 }, (error, _stdout, stderr) => {
      if (error) reject(new Error(`Time Machine was not told to leave out the VM's disks: ${String(stderr).trim() || error.message}`));
      else resolve();
    });
  });
}

/** *folder*, made where it is not, kept out of Time Machine by *run* (tmutil's arguments), once. */
export async function keepOutOfTimeMachine(folder: string, run: (args: string[]) => Promise<void> = tmutil): Promise<void> {
  const mark = join(folder, MARK);
  if (existsSync(mark)) return;
  mkdirSync(folder, { recursive: true, mode: 0o700 });
  await run(["addexclusion", folder]);
  writeFileSync(mark, "");
}
