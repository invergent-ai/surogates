// surogate-helper, the app's own program for what only the OS's own frameworks answer: on macOS (native/helper),
// this Mac's name, the company's certificate authority, and this user's processes; on Windows, surogate-helper.exe
// (native/windows/helper), with the same contract, and a program's signer and its App Paths key. On macOS it is
// started as every program the app starts (clean-child.ts); on Windows by itself, as the one program through which
// every other is started there. Either way with no environment of the app's. Electron-free.

import { execFile, spawnSync } from "node:child_process";

import { cleanly } from "./clean-child.js";
import { helperAt } from "./helper-path.js";
import { forThisOs, notBuilt } from "./platform.js";

export { helperAt };

export const HELPER = helperAt(import.meta.dirname);

// How the helper is started, and what it is started with: the environment, which on Windows holds the Windows folder
// alone, as Windows's own libraries read it from SystemRoot (Unsure: Microsoft does not say which of them read it; a
// process started without it is said to fail in some, Python's documentation, subprocess.Popen, env); and on Windows
// no window: an app with no console of its own, as the installed app started from the Start menu has none, would
// otherwise give a console program a new console window at each run (Node, child_process, windowsHide; the helper is
// a Windows-subsystem program besides, native/windows/helper).
interface Start {
  start(program: string, args: string[]): [file: string, args: string[]];
  options: { env: NodeJS.ProcessEnv; windowsHide?: boolean };
}
const posixStart: Start = { start: (program, args) => cleanly(program, args), options: { env: { PATH: "/usr/bin:/bin" } } };
const START: Start = forThisOs<Start>({
  linux: posixStart,
  darwin: posixStart,
  win32: {
    start: (program, args) => {
      if (!/^[A-Za-z]:\\/.test(program)) throw new Error(`${program} is not named by its whole path`);
      return [program, args];
    },
    options: { env: process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {}, windowsHide: true },
  },
}) ?? { start: () => notBuilt("Starting Surogate Desktop's helper"), options: { env: {} } };

// More than any answer of the helper's takes: a busy Mac's processes, with their arguments.
const MAX_BYTES = 32 * 1024 * 1024;
const TIMEOUT_MS = 10_000;

// Why a run of *program* gave no answer, in its own words where it said some.
const failed = (program: string, args: string[], stderr: string, error: Error | undefined): Error =>
  new Error(`${program} ${args.join(" ")} failed: ${stderr.trim().split("\n").at(-1) || error?.message || "no answer"}`);

/** What *program* (the helper) prints for *args*, waited for on this thread: for an answer a start needs before it goes on. */
export function helperSync(args: string[], program = HELPER): string {
  const done = spawnSync(...START.start(program, args), { ...START.options, encoding: "utf8", timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES });
  if (done.status !== 0) throw failed(program, args, done.stderr ?? "", done.error);
  return done.stdout;
}

/** What *program* (the helper) prints for *args*. */
export function helper(args: string[], program = HELPER): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(...START.start(program, args), { ...START.options, encoding: "utf8", timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES }, (error, stdout, stderr) => {
      if (error) reject(failed(program, args, stderr, error));
      else resolve(stdout);
    });
  });
}
