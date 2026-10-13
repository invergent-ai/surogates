// Windows's FdPaths: the file host's containment there, since Windows has no srt. Every file of a folder is reached
// through ntat (ntat.ts), one name at a time, each opened relative to the handle of the folder before it, never through
// a reparse point and never by a name Windows aliases (names-win32.ts). Node's own fs calls are never used on a
// folder's file: on Windows they follow every reparse point (libuv's fs__open never passes FILE_FLAG_OPEN_REPARSE_POINT;
// only its lstat and readlink do). A descriptor is node's own, so every fs call that takes one works on what the walk
// opened, and closeSync closes it.
//
// A path is a session's, POSIX: a folder's path in the guest, /mnt/<drive>/<its path>, its drive letter lower-cased
// and its names as they are on disk (the server, the model and the guest name it so). A path is reached only in a
// folder this process bound (bind), from that folder's handle, held from the bind on: one moved away is still the one
// worked in, and another put at its path is none of it. A folder above a bound one is a folder and nothing more of it,
// as the sandbox shows one on Linux; a path anywhere else is not there. A bound folder's own path is looked at where it
// lies now (lstat, stat), so that the helper sees a folder moved away and another put at its path (helper.ts), and only
// while that is the one bound.
//
// The check is the open: a component that is a reparse point is refused as a link, the answer Linux's O_NOFOLLOW gives,
// whatever its tag, but the cloud files' placeholders (OneDrive's), which are files and folders. A link in the folder
// that leads inside it is refused too, where Linux follows it.

import { createHash } from "node:crypto";
import { type BigIntStats, closeSync, constants, type Dirent, fchmodSync, fstatSync, readSync, type Stats, writeSync } from "node:fs";
import { posix } from "node:path";
import { getSystemErrorMap, getSystemErrorMessage } from "node:util";

import { osError, sandboxError } from "./answers.js";
import type { FdPaths, FileSearch, Filter, Listing, Where } from "./fd-paths.js";
import { WIN32_FLAGS } from "./flags.js";
import { descendsHidden, included } from "./glob.js";
import { walked } from "./identity.js";
import { nameRefusal, windowsTakes } from "./names-win32.js";
import { type FileId, ntat, type NtatConstants, type NtatEntry, type NtatError, type NtatQuery } from "./ntat.js";

const k = (): NtatConstants => ntat().constants;
const SHARE_ALL = (): number => k().FILE_SHARE_READ | k().FILE_SHARE_WRITE | k().FILE_SHARE_DELETE;
// What a pin shares: no one may ask for DELETE, so nothing it holds can be renamed or removed (NtCreateFile's remarks on
// ShareAccess: an open without FILE_SHARE_DELETE fails each later one that asks for DELETE). A pin asks for some of the
// file's data, a folder's traversal or a file's read: an open that asks for its attributes alone has its sharing neither
// checked nor kept (IoCheckShareAccess; measured on the runner, where such a pin let its file be renamed).
const SHARE_PINNED = (): number => k().FILE_SHARE_READ | k().FILE_SHARE_WRITE;
const PIECE = 1024 * 1024;
// Between 1601 and 1970, in Windows's 100 ns.
const EPOCH = 116_444_736_000_000_000n;
// The descriptors handed looks at: node's C runtime takes no more (_setmaxstdio's ceiling).
const MOST_DESCRIPTORS = 8192;

/** A folder's path in the guest as this computer's own: /mnt/c/Users/Ana is C:\Users\Ana; null for any other path. */
export function hostPathOf(guest: string): string | null {
  const drive = /^\/mnt\/([a-z])(?=\/|$)/.exec(guest);
  if (!drive) return null;
  return `${drive[1]!.toUpperCase()}:\\${guest.slice(drive[0].length + 1).split("/").join("\\")}`;
}

/** This computer's path, as Windows gives one (\\?\C:\...), as the guest's; null for one on no drive. */
export function guestPathOf(host: string): string | null {
  const drive = /^(?:\\\\\?\\)?([A-Za-z]):(\\.*)?$/.exec(host);
  if (!drive) return null;
  const rest = (drive[2] ?? "").split("\\").filter((name) => name !== "");
  return posix.join("/mnt", drive[1]!.toLowerCase(), ...rest);
}

// A failure as Node's are: its code, libuv's errno for it, and the call.
let errnos: Map<string, number> | null = null;
function nodeError(code: string, syscall: string, path: string): NodeJS.ErrnoException {
  errnos ??= new Map([...getSystemErrorMap()].map(([errno, [name]]) => [name, errno]));
  const errno = errnos.get(code) ?? -1;
  return Object.assign(new Error(`${code}: ${getSystemErrorMessage(errno)}, ${syscall} '${path}'`), { errno, code, syscall, path });
}

// What ntat refused for a name Windows aliases, or that opened as another: the file tools' words for it.
function told(error: unknown, name: string): unknown {
  const refused = (error as NtatError | undefined)?.refused;
  return refused === "name" || refused === "alias" ? sandboxError(nameRefusal(name)) : error;
}

interface Root {
  guest: string;
  host: string;
  fd: number;
  id: string;
}
const roots: Root[] = [];

/**
 * A file's identity, as identity.ts says Windows's is: dev, the volume serial's low 32 bits; ino, the first 52 bits of
 * SHA-256 over the serial (8 bytes, little-endian) and the 128-bit ID.
 */
export function identityFrom(raw: FileId): { dev: number; ino: number } {
  const serial = Buffer.alloc(8);
  serial.writeBigUInt64LE(raw.volume);
  const digest = createHash("sha256").update(serial).update(raw.id).digest();
  return { dev: Number(raw.volume & 0xffff_ffffn), ino: Number(digest.readBigUInt64BE(0) >> 12n) };
}
const idOf = (raw: NtatQuery): string => `${raw.volume}:${raw.id.toString("hex")}`;

// Whether *raw* is a reparse point the walk does not go through: any but a placeholder of the cloud files.
const linkIn = (raw: { tag: number | null }): boolean => raw.tag !== null && (raw.tag & k().IO_REPARSE_TAG_CLOUD_MASK) >>> 0 !== k().IO_REPARSE_TAG_CLOUD;

// A stat as Node's on Windows has it (libuv's fs__stat_handle): a link, a folder (its size 0) or a file; read-only as
// FILE_ATTRIBUTE_READONLY says, every class alike; the times of FILE_BASIC_INFO; and the file's identity.
function statsOf(raw: NtatQuery, bigint: true): BigIntStats;
function statsOf(raw: NtatQuery, bigint: false): Stats;
function statsOf(raw: NtatQuery, bigint: boolean): Stats | BigIntStats {
  const kind = linkIn(raw) ? constants.S_IFLNK : raw.directory ? constants.S_IFDIR : constants.S_IFREG;
  const mode = kind | ((raw.attributes & k().FILE_ATTRIBUTE_READONLY) !== 0 ? 0o444 : 0o666);
  const size = kind === constants.S_IFDIR ? 0n : raw.size;
  const { dev, ino } = identityFrom(raw);
  const times = Object.fromEntries((
    [["atime", raw.accessed], ["mtime", raw.written], ["ctime", raw.changed], ["birthtime", raw.created]] as const
  ).flatMap(([name, ticks]): Array<[string, unknown]> => {
    const ns = (ticks - EPOCH) * 100n;
    const ms = Number(ns / 1_000_000n) + Number(ns % 1_000_000n) / 1e6;
    return bigint ? [[`${name}Ns`, ns], [`${name}Ms`, ns / 1_000_000n], [name, new Date(ms)]] : [[`${name}Ms`, ms], [name, new Date(ms)]];
  }));
  const n = bigint ? (value: number | bigint) => BigInt(value) : (value: number | bigint) => Number(value);
  const is = (type: number) => () => kind === type;
  return walked({
    dev: n(dev), ino: n(ino), mode: n(mode), nlink: n(raw.links), uid: n(0), gid: n(0), rdev: n(0), size: n(size), blksize: n(4096),
    blocks: n(raw.allocated / 512n), ...times, isFile: is(constants.S_IFREG), isDirectory: is(constants.S_IFDIR),
    isSymbolicLink: is(constants.S_IFLNK), isFIFO: () => false, isSocket: () => false, isBlockDevice: () => false, isCharacterDevice: () => false,
  } as unknown as Stats);
}

// An entry as Node's Dirent has it: a link for a reparse point the walk does not go through, else a folder or a file.
function direntOf([name, attributes, tag]: NtatEntry): Dirent<Buffer> {
  const link = (attributes & k().FILE_ATTRIBUTE_REPARSE_POINT) !== 0 && linkIn({ tag });
  const folder = !link && (attributes & k().FILE_ATTRIBUTE_DIRECTORY) !== 0;
  return {
    name, isFile: () => !link && !folder, isDirectory: () => folder, isSymbolicLink: () => link, isFIFO: () => false, isSocket: () => false,
    isBlockDevice: () => false, isCharacterDevice: () => false,
  } as unknown as Dirent<Buffer>;
}

// Where a call acts: in the folder open as *dir*, on *name*, or on that folder itself (null); *close* lets go of what
// was opened to get there. *root* where the Where was a bound folder's own path.
interface Spot {
  dir: number;
  name: string | null;
  close(): void;
  root?: Root;
}

const ABOVE = Symbol("above");

// The bound folder *path* lies in, the deepest, and the names in it on the way; ABOVE for a folder a bound one lies in;
// null for a path in none. Every name is checked as Windows names are before anything is opened.
function located(path: string): { root: Root; parts: string[] } | typeof ABOVE | null {
  let found: Root | undefined;
  for (const root of roots) {
    if ((path === root.guest || path.startsWith(`${root.guest}/`)) && (!found || root.guest.length > found.guest.length)) found = root;
  }
  if (!found) return roots.some((root) => path === "/" || root.guest.startsWith(`${path}/`)) ? ABOVE : null;
  const parts = path.slice(found.guest.length + 1).split("/").filter((name) => name !== "" && name !== ".");
  for (const name of parts) {
    if (name === "..") throw nodeError("EINVAL", "open", path);
    if (!windowsTakes(name)) throw sandboxError(nameRefusal(name));
  }
  return { root: found, parts };
}

// A descriptor of the folder *parts* lead to from *dir*, each opened relative to the one before: the caller's own, to
// close. Where *parts* are none, *dir* itself, which is not.
function enter(dir: number, parts: string[], share = SHARE_ALL()): { fd: number; owned: boolean } {
  let at = dir;
  let owned = false;
  for (const name of parts) {
    let next: number;
    try {
      next = ntat().openAt(at, name, k().FILE_TRAVERSE, k().FILE_OPEN, k().FILE_DIRECTORY_FILE, share, 0, false);
    } catch (error) {
      throw told(error, name);
    } finally {
      if (owned) closeSync(at);
    }
    at = next;
    owned = true;
  }
  return { fd: at, owned };
}

function spot(where: Where, syscall: string): Spot | typeof ABOVE {
  if (typeof where !== "string") {
    if (where.name !== undefined && !windowsTakes(where.name)) throw sandboxError(nameRefusal(where.name));
    return { dir: where.dir, name: where.name ?? null, close: () => {} };
  }
  const found = located(where);
  if (found === null) throw nodeError("ENOENT", syscall, where);
  if (found === ABOVE) return ABOVE;
  const { root, parts } = found;
  if (parts.length === 0) return { dir: root.fd, name: null, close: () => {}, root };
  const { fd, owned } = enter(root.fd, parts.slice(0, -1));
  return { dir: fd, name: parts.at(-1)!, close: () => owned && closeSync(fd) };
}

function within<T>(where: Where, syscall: string, call: (at: Spot) => T, above?: () => T): T {
  const at = spot(where, syscall);
  if (at === ABOVE) {
    if (above) return above();
    throw nodeError("EACCES", syscall, typeof where === "string" ? where : "");
  }
  try {
    return call(at);
  } finally {
    at.close();
  }
}

// One name opened in the folder at *at*: with no name, that folder again, as a handle of its own.
function openIn(at: Spot, access: number, disposition: number, options: number, share = SHARE_ALL(), attributes = 0, links = false): number {
  if (at.name === null) {
    if (disposition === k().FILE_CREATE) throw nodeError("EEXIST", "open", typeof at.root?.guest === "string" ? at.root.guest : "");
    return ntat().reopen(at.dir, access, share);
  }
  try {
    return ntat().openAt(at.dir, at.name, access, disposition, options, share, attributes, links);
  } catch (error) {
    throw told(error, at.name);
  }
}

// What a bound folder's own path holds now: looked at anew, and answered only while it is the folder bound.
function lookedAt(root: Root): NtatQuery {
  const fd = ntat().openRoot(`\\\\?\\${root.host}`);
  try {
    const raw = ntat().query(fd);
    if (idOf(raw) !== root.id) throw nodeError("ENOENT", "lstat", root.guest);
    return raw;
  } finally {
    closeSync(fd);
  }
}

// A folder above a bound one: a folder, and nothing more of it.
function aboveStats(bigint: boolean): Stats & BigIntStats {
  const zero = bigint ? 0n : 0;
  const when = new Date(0);
  return {
    dev: zero, ino: zero, mode: bigint ? BigInt(constants.S_IFDIR) : constants.S_IFDIR, nlink: bigint ? 1n : 1, uid: zero, gid: zero, rdev: zero,
    size: zero, blksize: zero, blocks: zero, atimeMs: zero, mtimeMs: zero, ctimeMs: zero, birthtimeMs: zero, atime: when, mtime: when, ctime: when,
    birthtime: when, ...(bigint ? { atimeNs: 0n, mtimeNs: 0n, ctimeNs: 0n, birthtimeNs: 0n } : {}), isFile: () => false, isDirectory: () => true,
    isSymbolicLink: () => false, isFIFO: () => false, isSocket: () => false, isBlockDevice: () => false, isCharacterDevice: () => false,
  } as unknown as Stats & BigIntStats;
}

function queried(where: Where, links: boolean, syscall: string): NtatQuery | typeof ABOVE {
  return within<NtatQuery | typeof ABOVE>(where, syscall, (at) => {
    if (at.name === null) return at.root ? lookedAt(at.root) : ntat().query(at.dir);
    const fd = openIn(at, 0, k().FILE_OPEN, 0, SHARE_ALL(), 0, links);
    try {
      return ntat().query(fd);
    } finally {
      closeSync(fd);
    }
  }, () => ABOVE);
}

// Node's open flags on Windows (fs.constants, the C runtime's _O_* numbers), as NtCreateFile's access, disposition
// and options, as libuv's fs__open makes them: O_APPEND writes at the end alone (FILE_APPEND_DATA without
// FILE_WRITE_DATA).
function asNt(flags: number): { access: number; disposition: number; options: number } {
  const { O_WRONLY, O_RDWR, O_APPEND, O_CREAT, O_EXCL, O_TRUNC } = constants;
  const both = O_WRONLY | O_RDWR;
  let access = (flags & both) === O_WRONLY ? k().FILE_GENERIC_WRITE : (flags & both) === O_RDWR ? k().FILE_GENERIC_READ | k().FILE_GENERIC_WRITE : k().FILE_GENERIC_READ;
  if ((flags & O_APPEND) !== 0) access = (access & ~k().FILE_WRITE_DATA) | k().FILE_APPEND_DATA;
  const create = (flags & O_CREAT) !== 0;
  const disposition = create && (flags & O_EXCL) !== 0 ? k().FILE_CREATE
    : create && (flags & O_TRUNC) !== 0 ? k().FILE_OVERWRITE_IF
    : create ? k().FILE_OPEN_IF
    : (flags & O_TRUNC) !== 0 ? k().FILE_OVERWRITE
    : k().FILE_OPEN;
  return { access: access >>> 0, disposition, options: (flags & WIN32_FLAGS.O_DIRECTORY) !== 0 ? k().FILE_DIRECTORY_FILE : 0 };
}

const open = (where: Where, flags: number, mode = 0o666): number => within(where, "open", (at) => {
  const { access, disposition, options } = asNt(flags);
  // A file made without a write bit is made read-only, as libuv makes it.
  const attributes = (mode & 0o200) === 0 ? k().FILE_ATTRIBUTE_READONLY : k().FILE_ATTRIBUTE_NORMAL;
  return openIn(at, access, disposition, options, SHARE_ALL(), attributes);
});

function stat(where: Where): Stats;
function stat(where: Where, options: { bigint: true }): BigIntStats;
function stat(where: Where, options?: { bigint: true }): Stats | BigIntStats {
  const raw = queried(where, false, "stat");
  if (raw === ABOVE) return aboveStats(options?.bigint === true);
  return options?.bigint ? statsOf(raw, true) : statsOf(raw, false);
}

function listing(dir: number): Listing {
  const own = ntat().reopen(dir, k().FILE_LIST_DIRECTORY, SHARE_ALL());
  let entries: NtatEntry[] = [];
  let next = 0;
  let first = true;
  let done = false;
  return {
    readSync() {
      while (next === entries.length && !done) {
        const read = ntat().list(own, first);
        first = false;
        if (read === null) done = true;
        else [entries, next] = [read, 0];
      }
      return next < entries.length ? direntOf(entries[next++]!) : null;
    },
    closeSync: () => closeSync(own),
  };
}

const hold = (where: Where): number => within(where, "open", (at) => openIn(at, k().FILE_TRAVERSE, k().FILE_OPEN, k().FILE_DIRECTORY_FILE));

// The two names a move takes, each where it lies, and what it opened to get there.
function moved(from: Where, to: Where, syscall: string, call: (from: Spot & { name: string }, to: Spot & { name: string }) => void): void {
  within(from, syscall, (source) => within(to, syscall, (target) => {
    if (source.name === null || target.name === null) throw nodeError("EBUSY", syscall, typeof from === "string" ? from : "");
    try {
      call(source as Spot & { name: string }, target as Spot & { name: string });
    } catch (error) {
      throw told(error, source.name);
    }
  }));
}

function removed(where: Where, folder: boolean): void {
  within(where, folder ? "rmdir" : "unlink", (at) => {
    if (at.name === null) throw nodeError("EBUSY", folder ? "rmdir" : "unlink", typeof where === "string" ? where : "");
    try {
      ntat().unlinkAt(at.dir, at.name, folder);
    } catch (error) {
      throw told(error, at.name);
    }
  });
}

// copyFileSync's work between two descriptors: the bytes, and whether the source is read-only.
function copyBetween(from: number, to: number): void {
  const buffer = Buffer.allocUnsafe(PIECE);
  for (let read = readSync(from, buffer, 0, PIECE, null); read > 0; read = readSync(from, buffer, 0, PIECE, null)) {
    for (let wrote = 0; wrote < read;) wrote += writeSync(to, buffer, wrote, read - wrote);
  }
  fchmodSync(to, fstatSync(from).mode & 0o777);
}

// A search gives ripgrep only files, never a folder: ripgrep opens each path it is given, and a folder it was given it
// would list and then open each entry by path, following a link swapped in behind it. So the helper lists the tree
// itself here, by handle (the walk refuses every reparse point), and holds each file it gives ripgrep without delete
// sharing, with each folder on the way held too, so that nothing ripgrep opens can be renamed, removed or made a link
// until the batch ends ("a directory cannot be renamed if it or any of its subdirectories contains a file that has open
// handles", FILE_RENAME_INFORMATION's remarks). The glob is applied as ripgrep applies its -g on Linux (glob.ts): the
// oracle is ripgrep's own output (file-host-win32.test.ts). Files come in batches, so the open handles stay bounded.
const BATCH_FILES = 256;
// Room for the batch's paths on a command line, under the 32 767-unit limit, leaving the base arguments their share.
const BATCH_CHARS = 24_000;

// A directory the walk is in: its handle, held without delete sharing; its entries; and its path from the search root.
interface WalkLevel {
  fd: number;
  entries: Listing;
  rel: string;
}

function searchFiles(key: string, filter: Filter): FileSearch {
  const found = located(key);
  if (found === null || found === ABOVE) throw nodeError("ENOENT", "open", key);
  const { root, parts } = found;
  const rootHost = root.host.replaceAll("\\", "/");
  const positive = filter.glob !== null && !filter.glob.startsWith("!");
  const levels: WalkLevel[] = [];
  let single: { fd: number; path: string } | null = null;
  let pending: number[] = [];
  let started = false;

  const closeLevel = (level: WalkLevel) => {
    try {
      level.entries.closeSync();
    } catch {
      // Gone.
    }
    try {
      closeSync(level.fd);
    } catch {
      // Gone.
    }
  };
  const drop = (fds: number[]) => {
    for (const fd of fds.splice(0)) {
      try {
        closeSync(fd);
      } catch {
        // Gone.
      }
    }
  };

  // The key's own node, reached by the walk from the bound root, each component held without delete sharing: a folder
  // the walk lists, a file the batch of one. Each component is checked as every name is (openAt), and no reparse point
  // on the way is followed.
  const begin = () => {
    let dir = ntat().reopen(root.fd, k().FILE_TRAVERSE, SHARE_PINNED());
    for (let i = 0; i < parts.length - 1; i += 1) {
      let next: number;
      try {
        next = ntat().openAt(dir, parts[i]!, k().FILE_TRAVERSE, k().FILE_OPEN, k().FILE_DIRECTORY_FILE, SHARE_PINNED(), 0, false);
      } catch (error) {
        closeSync(dir);
        throw told(error, parts[i]!);
      }
      closeSync(dir);
      dir = next;
    }
    if (parts.length === 0) {
      levels.push({ fd: dir, entries: listing(dir), rel: "" });
      return;
    }
    // The walk's paths are from the bound root, not from the searched subfolder: its own path from the root seeds them.
    const last = parts.at(-1)!;
    let node: number;
    try {
      node = ntat().openAt(dir, last, k().FILE_READ_DATA, k().FILE_OPEN, 0, SHARE_PINNED(), 0, false);
    } catch (error) {
      closeSync(dir);
      throw told(error, last);
    }
    closeSync(dir);
    if (ntat().query(node).directory) levels.push({ fd: node, entries: listing(node), rel: parts.join("/") });
    else single = { fd: node, path: `${rootHost}/${parts.join("/")}` };
  };

  return {
    root: rootHost,
    next() {
      drop(pending);
      if (!started) {
        started = true;
        begin();
      }
      if (single) {
        const one = single;
        single = null;
        pending = [one.fd];
        return { paths: [one.path], release: () => drop(pending) };
      }
      const paths: string[] = [];
      const fds: number[] = [];
      let chars = 0;
      while (levels.length > 0 && paths.length < BATCH_FILES && chars < BATCH_CHARS) {
        const level = levels.at(-1)!;
        let entry: Dirent<Buffer> | null;
        try {
          entry = level.entries.readSync();
        } catch {
          entry = null; // what it could not read of a folder is left out, as the walk kind leaves it
        }
        if (entry === null) {
          levels.pop();
          closeLevel(level);
          continue;
        }
        const name = entry.name.toString("utf8");
        // A name that is not valid UTF-8 does not survive the round trip, as the walk kind and ripgrep both leave it out.
        if (!Buffer.from(name, "utf8").equals(entry.name)) continue;
        const rel = level.rel ? `${level.rel}/${name}` : name;
        // A reparse point is not followed, as ripgrep follows no link in its walk.
        if (entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) {
          // A hidden folder is skipped, as ripgrep skips one, unless the glob whitelists it.
          if (name.startsWith(".") && !descendsHidden(`/${rel}`, filter.glob)) continue;
          let fd: number;
          try {
            fd = ntat().openAt(level.fd, name, k().FILE_TRAVERSE, k().FILE_OPEN, k().FILE_DIRECTORY_FILE, SHARE_PINNED(), 0, false);
          } catch {
            continue; // gone, another's, or a reparse point met as the open resolves
          }
          levels.push({ fd, entries: listing(fd), rel });
          continue;
        }
        if (!entry.isFile() || filter.own.test(name)) continue;
        // A hidden file is skipped, as ripgrep skips one, unless a positive glob matches it.
        if (name.startsWith(".") && !positive) continue;
        if (!included(`/${rel}`, filter.glob)) continue;
        let fd: number;
        try {
          fd = ntat().openAt(level.fd, name, k().FILE_READ_DATA, k().FILE_OPEN, k().FILE_NON_DIRECTORY_FILE, SHARE_PINNED(), 0, false);
        } catch {
          continue; // gone, another's, or a reparse point
        }
        fds.push(fd);
        const path = `${rootHost}/${rel}`;
        paths.push(path);
        chars += path.length + 3;
      }
      if (paths.length === 0) return null;
      pending = fds;
      return { paths, release: () => drop(pending) };
    },
    close() {
      drop(pending);
      if (single) {
        try {
          closeSync(single.fd);
        } catch {
          // Gone.
        }
        single = null;
      }
      for (const level of levels.splice(0)) closeLevel(level);
    },
  };
}

export const win32FdPaths: FdPaths = {
  bind(folders) {
    for (const guest of folders) {
      if (roots.some((root) => root.guest === guest)) continue;
      const host = hostPathOf(guest);
      if (host === null || posix.normalize(guest) !== guest || guest.endsWith("/")) {
        throw new Error(`the file host on Windows works in a folder by its path in the guest, /mnt/<drive>/...: '${guest}'`);
      }
      const fd = ntat().openRoot(`\\\\?\\${host}`);
      try {
        // The path Windows gives the folder must be the one it was opened by: a link or a short name on its way would lead
        // a bound path somewhere it does not name.
        const real = ntat().pathOf(fd);
        if (real !== `\\\\?\\${host}`) throw new Error(`the folder ${guest} is not where its path leads, ${real}: a link or a short name is on its way`);
        roots.push({ guest, host, fd, id: idOf(ntat().query(fd)) });
      } catch (error) {
        closeSync(fd);
        throw error;
      }
    }
  },
  hold,
  open: (where, flags, mode) => open(where, flags, mode),
  lstat(where) {
    const raw = queried(where, true, "lstat");
    return raw === ABOVE ? aboveStats(true) : statsOf(raw, true);
  },
  stat,
  exists(where) {
    try {
      stat(where);
      return true;
    } catch {
      return false;
    }
  },
  readdir(where) {
    const held = hold(where);
    const names: string[] = [];
    try {
      const entries = listing(held);
      try {
        for (let entry = entries.readSync(); entry !== null; entry = entries.readSync()) names.push(entry.name.toString("utf8"));
      } finally {
        entries.closeSync();
      }
    } finally {
      closeSync(held);
    }
    return names;
  },
  access(where, mode) {
    const access = ((mode & constants.R_OK) !== 0 ? k().FILE_READ_DATA : 0) | ((mode & constants.W_OK) !== 0 ? k().FILE_WRITE_DATA : 0)
      | ((mode & constants.X_OK) !== 0 ? k().FILE_TRAVERSE : 0);
    closeSync(within(where, "access", (at) => openIn(at, access, k().FILE_OPEN, 0)));
  },
  mkdir: (where) => within(where, "mkdir", (at) => {
    if (at.name === null) throw nodeError("EEXIST", "mkdir", typeof where === "string" ? where : "");
    try {
      ntat().mkdirAt(at.dir, at.name);
    } catch (error) {
      throw told(error, at.name);
    }
  }, () => {
    throw nodeError("EEXIST", "mkdir", typeof where === "string" ? where : "");
  }),
  rmdir: (where) => removed(where, true),
  unlink: (where) => removed(where, false),
  // Windows keeps one mode bit, read-only (libuv's fs__fchmod sets FILE_ATTRIBUTE_READONLY by the owner's write bit).
  chmod(where, mode) {
    const fd = within(where, "chmod", (at) => openIn(at, k().FILE_WRITE_ATTRIBUTES, k().FILE_OPEN, 0));
    try {
      fchmodSync(fd, mode);
    } finally {
      closeSync(fd);
    }
  },
  link: (from, to) => moved(from, to, "link", (source, target) => ntat().linkAt(source.dir, source.name, target.dir, target.name)),
  rename: (from, to) => moved(from, to, "rename", (source, target) => ntat().renameAt(source.dir, source.name, target.dir, target.name, true)),
  copy(from, to) {
    const source = open(from, constants.O_RDONLY);
    try {
      const target = open(to, constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC);
      try {
        copyBetween(source, target);
      } finally {
        closeSync(target);
      }
    } finally {
      closeSync(source);
    }
  },
  // The walk follows no link, so no path is resolved through one: what a link holds is never read.
  readlink(path) {
    throw osError("ELOOP", path, "A link, which the file tools do not follow on Windows");
  },
  listing,
  pathOf(fd) {
    const raw = ntat().query(fd);
    if (raw.deletePending || raw.links === 0) return null;
    return guestPathOf(ntat().pathOf(fd));
  },
  handed() {
    const handed: number[] = [];
    for (let fd = 3; fd < MOST_DESCRIPTORS; fd += 1) if (ntat().inheritable(fd) === true) handed.push(fd);
    return handed;
  },
  shown(where) {
    if (typeof where === "string") return where;
    let folder: string;
    try {
      folder = win32FdPaths.pathOf(where.dir) ?? `<descriptor ${where.dir}>`;
    } catch {
      folder = `<descriptor ${where.dir}>`;
    }
    return where.name === undefined ? folder : `${folder}/${where.name}`;
  },
  // On Windows a program walks nothing in the folder: a search lists the tree itself and is given files (searchFiles).
  outside() {
    throw new Error("the Windows file host gives a program the files it lists, not a folder to walk");
  },
  searchFiles,
};
