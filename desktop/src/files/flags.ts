// Every open flag the file host spells by its number, for each OS, and nowhere else: Node's fs.constants names each
// OS's own common flags, and not these, and on Windows not O_DIRECTORY either. A flag's number on one OS can be another flag on the next: Linux's O_PATH is
// Darwin's O_SYMLINK, which opens a link itself where Linux's O_PATH with O_NOFOLLOW refuses one. Each OS's are a row
// of their own; files/ is on the agent disk, which holds no platform.js, so the row is picked here.

import { constants } from "node:fs";

import { type FsatConstants, fsat } from "./fsat.js";
import { notBuiltWords } from "./os-names.js";

// Linux's: a descriptor that holds a folder without reading it, so one this user may pass through but not list is
// held all the same; and close-on-exec, as the open flags in /proc/self/fdinfo show it.
export const LINUX_FLAGS = { O_PATH: 0o10000000, O_CLOEXEC: 0o2000000 } as const;

// Darwin's are its headers' own, as fsat gives them, read on the first open that needs one: Electron's processes
// import the file host's modules, and never load fsat.
let darwin: FsatConstants | null | undefined;
const darwinFlags = (): FsatConstants | null => (darwin === undefined ? (darwin = process.platform === "darwin" ? fsat().constants : null) : darwin);

// Windows's: no open of the file host's goes through Node there, and the walk follows no link at any name
// (fd-paths-win32.ts), so no flag says so. A folder is asked for by the C runtime's own _O_OBTAIN_DIR (<fcntl.h>), a
// number no other flag Node takes there has.
export const WIN32_FLAGS = { O_DIRECTORY: 0x2000 } as const;

interface Flags {
  NOFOLLOW: number;
  DIRECTORY: number;
}

const ROWS: Partial<Record<NodeJS.Platform, () => Flags>> = {
  linux: () => ({ NOFOLLOW: constants.O_NOFOLLOW, DIRECTORY: constants.O_DIRECTORY }),
  darwin: () => ({ NOFOLLOW: darwinFlags()!.O_NOFOLLOW_ANY, DIRECTORY: constants.O_DIRECTORY }),
  win32: () => ({ NOFOLLOW: 0, DIRECTORY: WIN32_FLAGS.O_DIRECTORY }),
};
// On an OS the file host is not built for yet, in notBuilt's words (os-names.ts).
const NOT_BUILT = notBuiltWords("The file host");
const row = ROWS[process.platform] ?? ((): never => {
  throw new Error(NOT_BUILT);
});

export const FLAGS = {
  /**
   * What a path the file helper opens never goes through, a link: at its last name, as O_NOFOLLOW; and on macOS at
   * any name on the way, O_NOFOLLOW_ANY (open(2): "do not follow symlinks in the entire path"), a second layer under
   * the sandbox's own. Every path the helper opens is one that resolves to itself, so no link on the way to it is one
   * it was given. Darwin takes one of the two, never both: an open with both fails EINVAL (measured on macOS 15).
   */
  get NOFOLLOW(): number {
    return row().NOFOLLOW;
  },
  /** That what is opened must be a folder, as O_DIRECTORY. */
  get DIRECTORY(): number {
    return row().DIRECTORY;
  },
};
