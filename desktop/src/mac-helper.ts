// surogate-helper (native/helper), the app's own program on macOS for what only Apple's frameworks
// answer: this Mac's name, the company's certificate authority, and this user's processes. It is
// started as every program the app starts (clean-child.ts), with no environment of the app's.
// Electron-free.

import { execFile, spawnSync } from "node:child_process";
import { join } from "node:path";

import { cleanly } from "./clean-child.js";

/**
 * The helper of the app whose code is in *dir*, its dist folder: in the app's bundle, beside the app's
 * own executable, as its code lives in Contents/Resources/app; in a development build, its build's own
 * (native/helper/build.sh).
 */
export function helperAt(dir: string): string {
  const bundle = /^(.+\.app)\/Contents\/Resources\/app\/dist$/.exec(dir)?.[1];
  return bundle ? join(bundle, "Contents", "MacOS", "surogate-helper") : join(dir, "..", "native", "helper", ".build", "surogate-helper");
}

export const HELPER = helperAt(import.meta.dirname);

const ENV = { PATH: "/usr/bin:/bin" };
// More than any answer of the helper's takes: a busy Mac's processes, with their arguments.
const MAX_BYTES = 32 * 1024 * 1024;
const TIMEOUT_MS = 10_000;

// Why a run of *program* gave no answer, in its own words where it said some.
const failed = (program: string, args: string[], stderr: string, error: Error | undefined): Error =>
  new Error(`${program} ${args.join(" ")} failed: ${stderr.trim().split("\n").at(-1) || error?.message || "no answer"}`);

/** What *program* (the helper) prints for *args*, waited for on this thread: for an answer a start needs before it goes on. */
export function helperSync(args: string[], program = HELPER): string {
  const done = spawnSync(...cleanly(program, args), { encoding: "utf8", env: ENV, timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES });
  if (done.status !== 0) throw failed(program, args, done.stderr ?? "", done.error);
  return done.stdout;
}

/** What *program* (the helper) prints for *args*. */
export function helper(args: string[], program = HELPER): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(...cleanly(program, args), { encoding: "utf8", env: ENV, timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES }, (error, stdout, stderr) => {
      if (error) reject(failed(program, args, stderr, error));
      else resolve(stdout);
    });
  });
}
