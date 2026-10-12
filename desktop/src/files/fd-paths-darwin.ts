// macOS's FdPaths: a file in a held folder is reached by Darwin's *at calls on the folder's descriptor, through
// fsat (fsat.ts), since macOS has no /proc/self/fd. A path is reached as it is, by Node's own calls, as Linux's
// FdPaths reaches one. A held folder is an O_RDONLY | O_DIRECTORY descriptor: Linux's O_PATH has no Darwin
// counterpart, so a folder this user may pass through but not list cannot be held here.

import {
  accessSync, type BigIntStats, chmodSync, closeSync, constants, copyFileSync, type Dirent, fchmodSync, fstatSync, linkSync, lstatSync,
  mkdirSync, openSync, readdirSync, readSync, renameSync, rmdirSync, type Stats, statSync, unlinkSync, writeSync,
} from "node:fs";

import type { FdPaths, Listing, Where } from "./fd-paths.js";
import { FLAGS } from "./flags.js";
import { fsat, type FsatStats } from "./fsat.js";

const at = () => fsat().constants;
const HOLD = () => constants.O_RDONLY | constants.O_DIRECTORY | FLAGS.NOFOLLOW;
// Node's own mode for a folder mkdirSync makes, and a file copyFileSync writes from.
const FOLDER_MODE = 0o777;
const PIECE = 1024 * 1024;

// The descriptor and name a call takes for *where*: a path, with AT_FDCWD; a name in a held folder; or, with no
// name, the held folder itself (null).
function split(where: Where): [dir: number, name: string | null] {
  if (typeof where === "string") return [at().AT_FDCWD, where];
  return [where.dir, where.name ?? null];
}

// A name for a call that takes one: with none, the held folder itself.
const named = (name: string | null): string => name ?? ".";

const kind = (mode: bigint, type: number): boolean => (Number(mode) & constants.S_IFMT) === type;
const ms = (sec: number, nsec: number): number => sec * 1e3 + nsec / 1e6;
const ns = (sec: number, nsec: number): bigint => BigInt(sec) * 1_000_000_000n + BigInt(nsec);

// A stat as Node's BigIntStats has it, its kind told from its mode.
function bigintStats(raw: FsatStats): BigIntStats {
  const { mode } = raw;
  const times = Object.fromEntries((["atime", "mtime", "ctime", "birthtime"] as const).flatMap((name) => {
    const sec = raw[`${name}Sec`];
    const nsec = raw[`${name}Nsec`];
    return [[`${name}Ns`, ns(sec, nsec)], [`${name}Ms`, BigInt(sec) * 1000n + BigInt(Math.trunc(nsec / 1e6))], [name, new Date(ms(sec, nsec))]];
  }));
  return {
    dev: raw.dev, ino: raw.ino, mode, nlink: raw.nlink, uid: raw.uid, gid: raw.gid, rdev: raw.rdev, size: raw.size, blksize: raw.blksize,
    blocks: raw.blocks, ...times, ...kinds(mode),
  } as unknown as BigIntStats;
}

// A stat as Node's Stats has it: each number as Node turns libuv's into one.
function numberStats(raw: FsatStats): Stats {
  const times = Object.fromEntries((["atime", "mtime", "ctime", "birthtime"] as const).flatMap((name) => {
    const sec = raw[`${name}Sec`];
    const nsec = raw[`${name}Nsec`];
    return [[`${name}Ms`, ms(sec, nsec)], [name, new Date(ms(sec, nsec))]];
  }));
  return {
    dev: Number(raw.dev), ino: Number(raw.ino), mode: Number(raw.mode), nlink: Number(raw.nlink), uid: Number(raw.uid), gid: Number(raw.gid),
    rdev: Number(raw.rdev), size: Number(raw.size), blksize: Number(raw.blksize), blocks: Number(raw.blocks), ...times, ...kinds(raw.mode),
  } as unknown as Stats;
}

function kinds(mode: bigint) {
  return {
    isFile: () => kind(mode, constants.S_IFREG),
    isDirectory: () => kind(mode, constants.S_IFDIR),
    isSymbolicLink: () => kind(mode, constants.S_IFLNK),
    isFIFO: () => kind(mode, constants.S_IFIFO),
    isSocket: () => kind(mode, constants.S_IFSOCK),
    isBlockDevice: () => kind(mode, constants.S_IFBLK),
    isCharacterDevice: () => kind(mode, constants.S_IFCHR),
  };
}

// An entry as Node's Dirent has it, its kind told from its d_type; DT_UNKNOWN is none of them, as in Node.
function dirent([name, type]: [Buffer, number]): Dirent<Buffer> {
  const { DT_REG, DT_DIR, DT_LNK, DT_FIFO, DT_SOCK, DT_BLK, DT_CHR } = at();
  return {
    name, isFile: () => type === DT_REG, isDirectory: () => type === DT_DIR, isSymbolicLink: () => type === DT_LNK, isFIFO: () => type === DT_FIFO,
    isSocket: () => type === DT_SOCK, isBlockDevice: () => type === DT_BLK, isCharacterDevice: () => type === DT_CHR,
  } as unknown as Dirent<Buffer>;
}

// copyFileSync's work between two descriptors: the bytes, and the source's mode.
function copyBetween(from: number, to: number): void {
  const buffer = Buffer.allocUnsafe(PIECE);
  for (let read = readSync(from, buffer, 0, PIECE, null); read > 0; read = readSync(from, buffer, 0, PIECE, null)) {
    for (let wrote = 0; wrote < read;) wrote += writeSync(to, buffer, wrote, read - wrote);
  }
  fchmodSync(to, fstatSync(from).mode & 0o7777);
}

const open = (where: Where, flags: number, mode = 0o666): number => {
  if (typeof where === "string") return openSync(where, flags, mode);
  const [dir, name] = split(where);
  return fsat().openat(dir, named(name), flags, mode);
};

export const darwinFdPaths: FdPaths = {
  hold: (where) => open(where, HOLD()),
  open: (where, flags, mode) => open(where, flags, mode),
  lstat(where) {
    if (typeof where === "string") return lstatSync(where, { bigint: true });
    const [dir, name] = split(where);
    return bigintStats(fsat().fstatat(dir, name, at().AT_SYMLINK_NOFOLLOW));
  },
  stat(where) {
    if (typeof where === "string") return statSync(where);
    const [dir, name] = split(where);
    return numberStats(fsat().fstatat(dir, name, 0));
  },
  access(where, mode) {
    if (typeof where === "string") return accessSync(where, mode);
    const [dir, name] = split(where);
    fsat().faccessat(dir, named(name), mode, 0);
  },
  mkdir(where) {
    if (typeof where === "string") return void mkdirSync(where);
    const [dir, name] = split(where);
    fsat().mkdirat(dir, named(name), FOLDER_MODE);
  },
  rmdir(where) {
    if (typeof where === "string") return rmdirSync(where);
    const [dir, name] = split(where);
    fsat().unlinkat(dir, named(name), at().AT_REMOVEDIR);
  },
  unlink(where) {
    if (typeof where === "string") return unlinkSync(where);
    const [dir, name] = split(where);
    fsat().unlinkat(dir, named(name), 0);
  },
  chmod(where, mode) {
    if (typeof where === "string") return chmodSync(where, mode);
    const [dir, name] = split(where);
    fsat().fchmodat(dir, name, mode, 0);
  },
  link(from, to) {
    if (typeof from === "string" && typeof to === "string") return linkSync(from, to);
    const [fromDir, fromName] = split(from);
    const [toDir, toName] = split(to);
    fsat().linkat(fromDir, named(fromName), toDir, named(toName), 0);
  },
  rename(from, to) {
    if (typeof from === "string" && typeof to === "string") return renameSync(from, to);
    const [fromDir, fromName] = split(from);
    const [toDir, toName] = split(to);
    fsat().renameatx(fromDir, named(fromName), toDir, named(toName), 0);
  },
  copy(from, to) {
    if (typeof from === "string" && typeof to === "string") return copyFileSync(from, to);
    const source = open(from, constants.O_RDONLY);
    try {
      const target = open(to, constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC, fstatSync(source).mode & 0o7777);
      try {
        copyBetween(source, target);
      } finally {
        closeSync(target);
      }
    } finally {
      closeSync(source);
    }
  },
  // One entry at a time, as Node's Dir hands them: a folder is read no further than its caller looks.
  listing(dir) {
    const listing = fsat().opendir(dir);
    return {
      readSync() {
        const entry = fsat().readdir(listing);
        return entry === null ? null : dirent(entry);
      },
      closeSync: () => fsat().closedir(listing),
    } satisfies Listing;
  },
  pathOf(fd) {
    let path: string;
    try {
      path = fsat().getpath(fd);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
    // Apple's fcntl(2) says only that F_GETPATH gives "the full path of the file descriptor": a name unlinked since
    // it was opened may still be the one it gives, and the file's one link left may be anywhere. So the path is
    // taken only while it names this very file.
    const open = fstatSync(fd, { bigint: true });
    let there: BigIntStats;
    try {
      there = lstatSync(path, { bigint: true });
    } catch {
      return null;
    }
    return open.nlink > 0n && there.dev === open.dev && there.ino === open.ino ? path : null;
  },
  handed() {
    const handed: number[] = [];
    for (const name of readdirSync("/dev/fd")) {
      const fd = Number(name);
      // The listing's own descriptor is closed since: it is no longer open, and is none.
      if (fd > 2 && fsat().cloexec(fd) === false) handed.push(fd);
    }
    return handed;
  },
  shown(where) {
    if (typeof where === "string") return where;
    let folder: string;
    try {
      folder = fsat().getpath(where.dir);
    } catch {
      folder = `/dev/fd/${where.dir}`;
    }
    return where.name === undefined ? folder : `${folder}/${where.name}`;
  },
};
