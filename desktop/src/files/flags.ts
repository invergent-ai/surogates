// Every open flag the file host spells by its number, for each OS, and nowhere else: Node's fs.constants names each
// OS's own common flags, and not these. A flag's number on one OS can be another flag on the next: Linux's O_PATH is
// Darwin's O_SYMLINK, which opens a link itself where Linux's O_PATH with O_NOFOLLOW refuses one.

import { constants } from "node:fs";

import { type FsatConstants, fsat } from "./fsat.js";

// Linux's: a descriptor that holds a folder without reading it, so one this user may pass through but not list is
// held all the same; and close-on-exec, as the open flags in /proc/self/fdinfo show it.
export const LINUX_FLAGS = { O_PATH: 0o10000000, O_CLOEXEC: 0o2000000 } as const;

// Darwin's are its headers' own, as fsat gives them, read on the first open that needs one: Electron's processes
// import the file host's modules, and never load fsat.
let darwin: FsatConstants | null | undefined;
const darwinFlags = (): FsatConstants | null => (darwin === undefined ? (darwin = process.platform === "darwin" ? fsat().constants : null) : darwin);

export const FLAGS = {
  /**
   * What a path the file helper opens never goes through, a link: at its last name, as O_NOFOLLOW; and on macOS at
   * any name on the way too, O_NOFOLLOW_ANY (open(2)), a second layer under the sandbox's own. Every path the helper
   * opens is one that resolves to itself, so no link on the way to it is one it was given.
   */
  get NOFOLLOW(): number {
    return constants.O_NOFOLLOW | (darwinFlags()?.O_NOFOLLOW_ANY ?? 0);
  },
};
