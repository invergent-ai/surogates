// Where the app's own helper is (native-helper.ts), by this OS, read before anything starts it: clean-child.ts starts
// every other program through it on Windows. Electron-free.

import { posix, win32 } from "node:path";

import { forThisOs, notBuilt } from "./platform.js";

// macOS's: in the app's bundle, beside the app's own executable, as its code lives in Contents/Resources/app; in a
// development build, its build's own (native/helper/build.sh). Linux has no helper of its own: there a development
// build's tests find macOS's where its build puts it.
function inBundle(dir: string): string {
  const bundle = /^(.+\.app)\/Contents\/Resources\/app\/dist$/.exec(dir)?.[1];
  return bundle ? posix.join(bundle, "Contents", "MacOS", "surogate-helper") : posix.join(dir, "..", "native", "helper", ".build", "surogate-helper");
}

/**
 * Windows's native *program* of the app whose code is in *dir*, its dist folder: in the installed app's bin folder, as
 * its code lives in resources\app (scripts/package-win.sh); in a development build, the release build of the
 * programs' Cargo workspace (native/windows).
 */
export function windowsProgramAt(dir: string, program: string): string {
  const install = /^(.+)\\resources\\app\\dist$/.exec(dir)?.[1];
  return install ? win32.join(install, "bin", program) : win32.join(dir, "..", "native", "windows", "target", "release", program);
}

// Windows's helper, beside the other programs.
const inInstall = (dir: string): string => windowsProgramAt(dir, "surogate-helper.exe");

/** The helper of the app whose code is in *dir*, its dist folder, by this OS. */
export const helperAt: (dir: string) => string = forThisOs({ linux: inBundle, darwin: inBundle, win32: inInstall })
  ?? (() => notBuilt("Surogate Desktop's helper"));
