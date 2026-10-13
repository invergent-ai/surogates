// What of the file kinds is each OS's own beyond how a folder's file is reached (fd-paths.ts) and opened (flags.ts):
// the names a write may not make there beyond protect.ts's, where the helper's rg is, and how rg is told to print a
// path. files/ is on the agent disk, which holds no platform.js, so the row is picked here.

import { statSync } from "node:fs";
import { win32 } from "node:path";

import { explorerActsOn } from "./explorer-names.js";
import { notBuiltWords } from "./os-names.js";

/** shutil.which's way of looking for a program on a PATH (operations.ts, findOnPath). */
export type Find = (name: string, path: string | undefined, cwd: string) => string | null;

export interface FileOs {
  /** Whether a write of *key* is refused for its last name: on Windows, a name Explorer acts on (explorer-names.ts). */
  explorer(key: string): boolean;
  /** The helper's rg, looked for on *path*, the helper's own PATH, from *folder*. */
  rg(path: string | undefined, folder: string, find: Find): string | null;
  /** What rg is told before the path it searches, so that it names what it finds as the file kinds name paths. */
  readonly rgArgs: readonly string[];
  /** A path rg's JSON names, as the file kinds name it, where rgArgs do not reach it; null where they do. */
  readonly rgPrinted: ((path: string) => string) | null;
}

const POSIX: FileOs = { explorer: () => false, rg: (path, folder, find) => find("rg", path, folder), rgArgs: [], rgPrinted: null };

// Windows's PATH: absolute entries apart by ';', each looked in for rg.exe, as the app's own rg is beside its node
// (hosts' toolsPath). A relative entry would be the helper's working folder, which is the user's folder: none is read.
function windowsRg(path: string | undefined): string | null {
  for (const entry of (path ?? "").split(";")) {
    if (!win32.isAbsolute(entry)) continue;
    const candidate = win32.join(entry, "rg.exe");
    try {
      if (statSync(candidate).isFile()) return candidate;
    } catch {
      // Not there.
    }
  }
  return null;
}

const ROWS: Partial<Record<NodeJS.Platform, FileOs>> = {
  linux: POSIX,
  darwin: POSIX,
  // rg prints Windows's paths with '\' unless told: the file kinds turn this computer's path into the guest's by its
  // start alone (edge.ts), so it prints '/'. Its JSON keeps '\' below the path it was given (measured with 15.2.0),
  // and no name on Windows holds one (names-win32.ts): each is a '/'.
  win32: {
    explorer: explorerActsOn, rg: (path) => windowsRg(path), rgArgs: ["--path-separator", "/"], rgPrinted: (path) => path.replaceAll("\\", "/"),
  },
};
// On an OS the file host is not built for yet, in notBuilt's words (os-names.ts).
const NOT_BUILT = notBuiltWords("The file host");
const refused = (): never => {
  throw new Error(NOT_BUILT);
};

export const fileOs: FileOs = ROWS[process.platform] ?? { explorer: refused, rg: refused, rgArgs: [], rgPrinted: null };
