// surogate-helper, the app's own program for what only the OS's own frameworks answer: on macOS (native/helper),
// this Mac's name, the company's certificate authority, and this user's processes; on Windows, surogate-helper.exe
// (native/windows/helper), with the same contract, and a program's signer and its App Paths key. On macOS it is
// started as every program the app starts (clean-child.ts); on Windows by itself, as the one program through which
// every other is started there. Either way with no environment of the app's. Electron-free.

import { execFile, spawnSync } from "node:child_process";
import { posix, win32 } from "node:path";

import { cleanly } from "./clean-child.js";
import { forThisOs, notBuilt } from "./platform.js";

// macOS's: in the app's bundle, beside the app's own executable, as its code lives in Contents/Resources/app; in a
// development build, its build's own (native/helper/build.sh). Linux has no helper of its own: there a development
// build's tests find macOS's where its build puts it.
function inBundle(dir: string): string {
  const bundle = /^(.+\.app)\/Contents\/Resources\/app\/dist$/.exec(dir)?.[1];
  return bundle ? posix.join(bundle, "Contents", "MacOS", "surogate-helper") : posix.join(dir, "..", "native", "helper", ".build", "surogate-helper");
}

// Windows's: in the installed app's bin folder, as its code lives in resources\app; in a development build, the
// release build of the programs' Cargo workspace (native/windows).
function inInstall(dir: string): string {
  const install = /^(.+)\\resources\\app\\dist$/.exec(dir)?.[1];
  return install ? win32.join(install, "bin", "surogate-helper.exe") : win32.join(dir, "..", "native", "windows", "target", "release", "surogate-helper.exe");
}

/** The helper of the app whose code is in *dir*, its dist folder, by this OS. */
export const helperAt: (dir: string) => string = forThisOs({ linux: inBundle, darwin: inBundle, win32: inInstall })
  ?? (() => notBuilt("Surogate Desktop's helper"));

export const HELPER = helperAt(import.meta.dirname);

// How the helper is started, and the environment it is given: Windows's own libraries read the Windows folder from
// SystemRoot, and nothing else of the app's is given. Unsure: Microsoft does not say which of them read it; a process
// started without it is said to fail in some (Python's documentation, subprocess.Popen, env).
interface Start {
  start(program: string, args: string[]): [file: string, args: string[]];
  env: NodeJS.ProcessEnv;
}
const posixStart: Start = { start: (program, args) => cleanly(program, args), env: { PATH: "/usr/bin:/bin" } };
const START: Start = forThisOs<Start>({
  linux: posixStart,
  darwin: posixStart,
  win32: {
    start: (program, args) => {
      if (!/^[A-Za-z]:\\/.test(program)) throw new Error(`${program} is not named by its whole path`);
      return [program, args];
    },
    env: process.env.SystemRoot ? { SystemRoot: process.env.SystemRoot } : {},
  },
}) ?? { start: () => notBuilt("Starting Surogate Desktop's helper"), env: {} };

// More than any answer of the helper's takes: a busy Mac's processes, with their arguments.
const MAX_BYTES = 32 * 1024 * 1024;
const TIMEOUT_MS = 10_000;

// Why a run of *program* gave no answer, in its own words where it said some.
const failed = (program: string, args: string[], stderr: string, error: Error | undefined): Error =>
  new Error(`${program} ${args.join(" ")} failed: ${stderr.trim().split("\n").at(-1) || error?.message || "no answer"}`);

/** What *program* (the helper) prints for *args*, waited for on this thread: for an answer a start needs before it goes on. */
export function helperSync(args: string[], program = HELPER): string {
  const done = spawnSync(...START.start(program, args), { encoding: "utf8", env: START.env, timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES });
  if (done.status !== 0) throw failed(program, args, done.stderr ?? "", done.error);
  return done.stdout;
}

/** What *program* (the helper) prints for *args*. */
export function helper(args: string[], program = HELPER): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(...START.start(program, args), { encoding: "utf8", env: START.env, timeout: TIMEOUT_MS, maxBuffer: MAX_BYTES }, (error, stdout, stderr) => {
      if (error) reject(failed(program, args, stderr, error));
      else resolve(stdout);
    });
  });
}
