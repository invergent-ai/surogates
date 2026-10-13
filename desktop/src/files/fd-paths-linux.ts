// Linux's FdPaths: a file in a held folder is reached by the descriptor's entry in /proc/self/fd, which leads to the
// very folder the descriptor holds, wherever that is now and whatever is at its path.

import {
  accessSync, chmodSync, constants, copyFileSync, existsSync, linkSync, lstatSync, mkdirSync, opendirSync, openSync, readdirSync, readFileSync,
  readlinkSync, renameSync, rmdirSync, statSync, unlinkSync,
} from "node:fs";

import type { FdPaths, Listing, Where } from "./fd-paths.js";
import { LINUX_FLAGS } from "./flags.js";

const HOLD = LINUX_FLAGS.O_PATH | constants.O_DIRECTORY | constants.O_NOFOLLOW;

const path = (where: Where): string =>
  typeof where === "string" ? where : `/proc/self/fd/${where.dir}${where.name === undefined ? "" : `/${where.name}`}`;

export const linuxFdPaths: FdPaths = {
  // The sandbox binds them.
  bind: () => {},
  hold: (where) => openSync(path(where), HOLD),
  open: (where, flags, mode) => (mode === undefined ? openSync(path(where), flags) : openSync(path(where), flags, mode)),
  lstat: (where) => lstatSync(path(where), { bigint: true }),
  stat: ((where: Where, options?: { bigint: true }) => (options ? statSync(path(where), options) : statSync(path(where)))) as FdPaths["stat"],
  exists: (where) => existsSync(path(where)),
  readdir: (where) => readdirSync(path(where)),
  access: (where, mode) => accessSync(path(where), mode),
  mkdir: (where) => void mkdirSync(path(where)),
  rmdir: (where) => rmdirSync(path(where)),
  unlink: (where) => unlinkSync(path(where)),
  chmod: (where, mode) => chmodSync(path(where), mode),
  link: (from, to) => linkSync(path(from), path(to)),
  rename: (from, to) => renameSync(path(from), path(to)),
  copy: (from, to) => copyFileSync(path(from), path(to)),
  readlink: (link) => readlinkSync(link, "utf8"),
  // @types/node names a Dir's entries as strings, and takes no "buffer" encoding; Node reads them as bytes with it.
  listing: (dir) => opendirSync(path({ dir }), { encoding: "buffer" as BufferEncoding }) as unknown as Listing,
  pathOf(fd) {
    const real = readlinkSync(`/proc/self/fd/${fd}`);
    // Unlinked since it was opened: its one link left may be anywhere. A file really named so is taken for one too.
    return real.endsWith(" (deleted)") ? null : real;
  },
  handed() {
    const handed: number[] = [];
    for (const name of readdirSync("/proc/self/fd")) {
      const fd = Number(name);
      if (fd <= 2) continue;
      try {
        const flags = Number.parseInt(/^flags:\s*([0-7]+)$/m.exec(readFileSync(`/proc/self/fdinfo/${fd}`, "utf8"))?.[1] ?? "", 8);
        if (!Number.isNaN(flags) && (flags & LINUX_FLAGS.O_CLOEXEC) === 0) handed.push(fd);
      } catch {
        // The listing's own descriptor, closed since.
      }
    }
    return handed;
  },
  shown: path,
  // In the sandbox, which binds the folder for the helper's life.
  outside: (path) => ({ path, empty: false, release: () => {} }),
  // rg walks the folder here, inside the sandbox.
  searchFiles: () => {
    throw new Error("the file host lists the tree itself only where no sandbox binds the folder");
  },
};
