// For the tests alone: a Plan 9 server (9P2000.L) on a Unix socket, in the place of the one Hyper-V's host
// serves a guest's shares from, which the stand-in for Hyper-V's devices bridges the guest's vsock port 564 to
// (standin.ts). Each share is a folder by its name, attached by the guest's mount as its aname: "<name>", or
// "<name>;uid=<n>;gid=<n>", whose files then show as that uid's and gid's, as the guest's p9 share asks of the
// host's server (guest/protocol.ts). A read-only share refuses every write. Its files are this process's
// user's on the host, and no walk leaves a share's folder.
//
//   node p9-server.mjs <socket>
//     stdin  {"add":"r1","path":"/abs/folder","readOnly":false}   stdout {"added":"r1"}
//     stdin  {"remove":"r1"}                                      stdout {"removed":"r1"}
//     stdout {"listening":true} once the socket takes connections

import { spawnSync } from "node:child_process";
import {
  closeSync, constants, fstatSync, fsyncSync, ftruncateSync, linkSync, lstatSync, lutimesSync, mkdirSync, openSync, readdirSync, readlinkSync,
  readSync, renameSync, rmdirSync, statfsSync, symlinkSync, unlinkSync, writeSync, chmodSync,
} from "node:fs";
import { createServer } from "node:net";
import { constants as os } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";

const shares = new Map(); // name -> { path, readOnly }
const errno = (code) => os.errno[code] ?? os.errno.EIO;

const QTDIR = 0x80, QTSYMLINK = 0x02;
const O_ACCMODE = 3, O_CREAT = 0o100, O_EXCL = 0o200, O_TRUNC = 0o1000, O_APPEND = 0o2000, O_NONBLOCK = 0o4000, O_DSYNC = 0o10000;
const O_DIRECTORY = 0o200000, O_SYNC = 0o4010000;
const KEPT = O_ACCMODE | O_TRUNC | O_APPEND | O_NONBLOCK | O_DSYNC | O_SYNC | O_EXCL;
const AT_REMOVEDIR = 0x200;

class Failure extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}
const fail = (code) => {
  throw new Failure(code);
};

// A message's fields, read in turn.
class Reader {
  constructor(buffer) {
    this.b = buffer;
    this.at = 0;
  }
  u8() { return this.b.readUInt8((this.at += 1) - 1); }
  u16() { return this.b.readUInt16LE((this.at += 2) - 2); }
  u32() { return this.b.readUInt32LE((this.at += 4) - 4); }
  u64() { return this.b.readBigUInt64LE((this.at += 8) - 8); }
  str() {
    const n = this.u16();
    return this.b.toString("latin1", this.at, (this.at += n));
  }
  bytes(n) { return this.b.subarray(this.at, (this.at += n)); }
}
// An answer's fields, written in turn.
class Writer {
  constructor() { this.parts = []; }
  u8(v) { const b = Buffer.alloc(1); b.writeUInt8(v); this.parts.push(b); return this; }
  u16(v) { const b = Buffer.alloc(2); b.writeUInt16LE(v); this.parts.push(b); return this; }
  u32(v) { const b = Buffer.alloc(4); b.writeUInt32LE(v >>> 0); this.parts.push(b); return this; }
  u64(v) { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt.asUintN(64, BigInt(v))); this.parts.push(b); return this; }
  str(s) { const b = Buffer.from(s, "latin1"); this.u16(b.length); this.parts.push(b); return this; }
  bytes(b) { this.parts.push(b); return this; }
  qid(st) {
    const type = st.isDirectory() ? QTDIR : st.isSymbolicLink() ? QTSYMLINK : 0;
    return this.u8(type).u32(Number(st.mtimeNs % 4294967296n)).u64(st.ino);
  }
  done(type, tag) {
    const body = Buffer.concat(this.parts);
    const head = Buffer.alloc(7);
    head.writeUInt32LE(body.length + 7);
    head.writeUInt8(type, 4);
    head.writeUInt16LE(tag, 5);
    return Buffer.concat([head, body]);
  }
}

// A name's bytes as they are, never decoded: each path here is a string of them one a character (latin1), as
// the wire gives them, and each is given to the file system as those bytes again.
const B = (path) => Buffer.from(path, "latin1");
const stat = (path) => lstatSync(B(path), { bigint: true });

// One connection: a guest's mount of a share.
function serve(socket) {
  const fids = new Map(); // fid -> { path, root, share, uid, gid, fd, listing }
  let msize = 65536;
  let pending = Buffer.alloc(0);

  const fidOf = (n) => fids.get(n) ?? fail("EBADF");
  // *name*, one component, in the folder of *fid*, never through a link: never out of its share.
  const inside = (fid, name) => {
    if (name === "" || name.includes("/") || name === "." || name === "..") fail("EINVAL");
    if (!stat(fid.path).isDirectory()) fail("ENOTDIR");
    return join(fid.path, name);
  };
  const writable = (fid) => {
    if (fid.share.readOnly) fail("EROFS");
  };
  const close = (fid) => {
    if (fid.fd !== undefined) closeSync(fid.fd);
    fid.fd = undefined;
  };

  const handlers = {
    // Tversion: msize[4] version[s]
    100: (r, w) => {
      msize = Math.min(r.u32(), 1024 * 1024);
      const version = r.str();
      for (const fid of fids.values()) close(fid);
      fids.clear();
      return [101, w.u32(msize).str(version.startsWith("9P2000.L") ? "9P2000.L" : "unknown")];
    },
    // Tattach: fid[4] afid[4] uname[s] aname[s] n_uname[4]
    104: (r, w) => {
      const fid = r.u32();
      r.u32();
      r.str();
      const [name, ...options] = r.str().split(";");
      const share = shares.get(name) ?? fail("ENOENT");
      const owner = Object.fromEntries(options.map((option) => option.split("=")));
      const uid = owner.uid === undefined ? process.getuid() : Number(owner.uid);
      const gid = owner.gid === undefined ? process.getgid() : Number(owner.gid);
      fids.set(fid, { path: share.path, root: share.path, share, uid, gid });
      return [105, w.qid(stat(share.path))];
    },
    // Tflush: oldtag[2]
    108: (r, w) => [109, w],
    // Twalk: fid[4] newfid[4] nwname[2] wname[s]...
    110: (r, w) => {
      const from = fidOf(r.u32());
      const newfid = r.u32();
      const names = Array.from({ length: r.u16() }, () => r.str());
      let path = from.path;
      const qids = [];
      for (const name of names) {
        if (name.includes("/")) fail("EINVAL");
        let next = name === ".." ? (path === from.root ? path : dirname(path)) : name === "." ? path : join(path, name);
        if (!next.startsWith(from.root)) next = from.root;
        let st;
        try {
          if (stat(path).isSymbolicLink()) fail("ENOTDIR");
          st = stat(next);
        } catch (error) {
          if (qids.length === 0) throw error;
          break;
        }
        qids.push(st);
        path = next;
      }
      if (qids.length === names.length) fids.set(newfid, { ...from, path, fd: undefined, listing: undefined });
      w.u16(qids.length);
      for (const st of qids) w.qid(st);
      return [111, w];
    },
    // Tlopen: fid[4] flags[4]
    12: (r, w) => {
      const fid = fidOf(r.u32());
      const flags = r.u32();
      const st = stat(fid.path);
      if ((flags & O_ACCMODE) !== 0 || flags & O_TRUNC) writable(fid);
      if (!st.isDirectory()) fid.fd = openSync(B(fid.path), (flags & KEPT & ~O_EXCL) | constants.O_NOFOLLOW);
      return [13, w.qid(st).u32(0)];
    },
    // Tlcreate: fid[4] name[s] flags[4] mode[4] gid[4]
    14: (r, w) => {
      const fid = fidOf(r.u32());
      const path = inside(fid, r.str());
      const flags = r.u32();
      const mode = r.u32();
      r.u32();
      writable(fid);
      fid.fd = openSync(B(path), (flags & KEPT) | O_CREAT | constants.O_NOFOLLOW, mode & 0o7777);
      fid.path = path;
      return [15, w.qid(stat(path)).u32(0)];
    },
    // Tsymlink: fid[4] name[s] symtgt[s] gid[4]
    16: (r, w) => {
      const fid = fidOf(r.u32());
      const path = inside(fid, r.str());
      const target = r.str();
      writable(fid);
      symlinkSync(B(target), B(path));
      return [17, w.qid(stat(path))];
    },
    // Tmknod: dfid[4] name[s] mode[4] major[4] minor[4] gid[4]
    18: (r, w) => {
      const fid = fidOf(r.u32());
      const path = inside(fid, r.str());
      const mode = r.u32();
      writable(fid);
      const kind = mode & 0o170000;
      if (kind === 0o010000) {
        if (spawnSync("mkfifo", ["-m", (mode & 0o7777).toString(8), B(path).toString()]).status !== 0) fail("EIO");
      } else if (kind === 0o100000 || kind === 0) {
        closeSync(openSync(B(path), O_CREAT | O_EXCL | 1, mode & 0o7777));
      } else {
        fail("EPERM");
      }
      return [19, w.qid(stat(path))];
    },
    // Trename: fid[4] dfid[4] name[s]
    20: (r, w) => {
      const fid = fidOf(r.u32());
      const to = inside(fidOf(r.u32()), r.str());
      writable(fid);
      renamed(fid.path, to);
      return [21, w];
    },
    // Treadlink: fid[4]
    22: (r, w) => [23, w.str(readlinkSync(B(fidOf(r.u32()).path), { encoding: "buffer" }).toString("latin1"))],
    // Tgetattr: fid[4] request_mask[8]
    24: (r, w) => {
      const fid = fidOf(r.u32());
      const st = fid.fd !== undefined ? fstatSync(fid.fd, { bigint: true }) : stat(fid.path);
      w.u64(0x7ffn).qid(st).u32(Number(st.mode)).u32(fid.uid).u32(fid.gid).u64(st.nlink).u64(st.rdev).u64(st.size).u64(st.blksize).u64(st.blocks);
      for (const ns of [st.atimeNs, st.mtimeNs, st.ctimeNs, st.birthtimeNs]) w.u64(ns / 1000000000n).u64(ns % 1000000000n);
      return [25, w.u64(0).u64(0)];
    },
    // Tsetattr: fid[4] valid[4] mode[4] uid[4] gid[4] size[8] atime_sec[8] atime_nsec[8] mtime_sec[8] mtime_nsec[8]
    26: (r, w) => {
      const fid = fidOf(r.u32());
      const valid = r.u32();
      const mode = r.u32();
      r.u32();
      r.u32();
      const size = r.u64();
      const atime = Number(r.u64()) + Number(r.u64()) / 1e9;
      const mtime = Number(r.u64()) + Number(r.u64()) / 1e9;
      // The owner is the share's: a chown changes nothing on the host, as no other user's file can be made.
      if (valid & ~(0x2 | 0x4 | 0x40)) writable(fid);
      // Never through a link: a link's own mode does not change.
      const link = stat(fid.path).isSymbolicLink();
      if (valid & 0x1 && !link) chmodSync(B(fid.path), mode & 0o7777);
      if (valid & 0x8) {
        if (link) fail("EINVAL");
        // On its own descriptor where it has one, as an ftruncate asks: the file may be one only that one may write.
        let done = false;
        if (fid.fd !== undefined) {
          try {
            ftruncateSync(fid.fd, Number(size));
            done = true;
          } catch {
            // Open for reading alone.
          }
        }
        if (!done) {
          const fd = openSync(B(fid.path), 1 | constants.O_NOFOLLOW);
          try {
            ftruncateSync(fd, Number(size));
          } finally {
            closeSync(fd);
          }
        }
      }
      if (valid & (0x10 | 0x20)) {
        const st = stat(fid.path);
        const now = Date.now() / 1000;
        const a = valid & 0x10 ? (valid & 0x80 ? atime : now) : Number(st.atimeNs) / 1e9;
        const m = valid & 0x20 ? (valid & 0x100 ? mtime : now) : Number(st.mtimeNs) / 1e9;
        lutimesSync(B(fid.path), a, m);
      }
      return [27, w];
    },
    // Txattrwalk: fid[4] newfid[4] name[s]: no attributes.
    30: (r) => {
      r.u32();
      r.u32();
      fail(r.str() === "" ? "EOPNOTSUPP" : "ENODATA");
    },
    32: () => fail("EOPNOTSUPP"),
    // Treaddir: fid[4] offset[8] count[4]
    40: (r, w) => {
      const fid = fidOf(r.u32());
      const offset = Number(r.u64());
      const count = r.u32();
      if (offset === 0 || !fid.listing) {
        if (!stat(fid.path).isDirectory()) fail("ENOTDIR");
        const names = [".", "..", ...readdirSync(B(fid.path), { encoding: "buffer" }).map((name) => name.toString("latin1"))];
        fid.listing = names.flatMap((name) => {
          try {
            const path = name === "." ? fid.path : name === ".." ? (fid.path === fid.root ? fid.path : dirname(fid.path)) : join(fid.path, name);
            return [{ name, st: stat(path) }];
          } catch {
            return [];
          }
        });
      }
      const entries = new Writer();
      let used = 0;
      for (let at = offset; at < fid.listing.length; at += 1) {
        const { name, st } = fid.listing[at];
        const size = 13 + 8 + 1 + 2 + name.length;
        if (used + size > count) break;
        const type = st.isDirectory() ? 4 : st.isSymbolicLink() ? 10 : st.isFIFO() ? 1 : st.isSocket() ? 12 : 8;
        entries.qid(st).u64(at + 1).u8(type).str(name);
        used += size;
      }
      const data = Buffer.concat(entries.parts);
      return [41, w.u32(data.length).bytes(data)];
    },
    // Tfsync: fid[4] datasync[4]
    50: (r, w) => {
      const fid = fidOf(r.u32());
      if (fid.fd !== undefined) fsyncSync(fid.fd);
      return [51, w];
    },
    // Tlock, Tgetlock: every lock is granted, and none is held.
    52: (r, w) => [53, w.u8(0)],
    54: (r, w) => {
      r.u32();
      r.u8();
      const start = r.u64();
      const length = r.u64();
      const pid = r.u32();
      return [55, w.u8(2).u64(start).u64(length).u32(pid).str(r.str())];
    },
    // Tlink: dfid[4] fid[4] name[s]
    70: (r, w) => {
      const dir = fidOf(r.u32());
      const fid = fidOf(r.u32());
      writable(dir);
      linkSync(B(fid.path), B(inside(dir, r.str())));
      return [71, w];
    },
    // Tmkdir: dfid[4] name[s] mode[4] gid[4]
    72: (r, w) => {
      const fid = fidOf(r.u32());
      const path = inside(fid, r.str());
      const mode = r.u32();
      writable(fid);
      mkdirSync(B(path), { mode: mode & 0o7777 });
      return [73, w.qid(stat(path))];
    },
    // Trenameat: olddirfid[4] oldname[s] newdirfid[4] newname[s]
    74: (r, w) => {
      const from = fidOf(r.u32());
      const old = inside(from, r.str());
      const to = inside(fidOf(r.u32()), r.str());
      writable(from);
      renamed(old, to);
      return [75, w];
    },
    // Tunlinkat: dirfd[4] name[s] flags[4]
    76: (r, w) => {
      const fid = fidOf(r.u32());
      const path = inside(fid, r.str());
      const flags = r.u32();
      writable(fid);
      if (flags & AT_REMOVEDIR) rmdirSync(B(path));
      else unlinkSync(B(path));
      return [77, w];
    },
    // Tstatfs: fid[4]
    8: (r, w) => {
      const s = statfsSync(B(fidOf(r.u32()).path), { bigint: true });
      return [9, w.u32(0x01021997).u32(Number(s.bsize)).u64(s.blocks).u64(s.bfree).u64(s.bavail).u64(s.files).u64(s.ffree).u64(0).u32(255)];
    },
    // Tread: fid[4] offset[8] count[4]
    116: (r, w) => {
      const fid = fidOf(r.u32());
      const offset = Number(r.u64());
      const count = Math.min(r.u32(), msize - 11);
      if (fid.fd === undefined) fail("EBADF");
      const data = Buffer.alloc(count);
      const got = readSync(fid.fd, data, 0, count, offset);
      return [117, w.u32(got).bytes(data.subarray(0, got))];
    },
    // Twrite: fid[4] offset[8] count[4] data[count]
    118: (r, w) => {
      const fid = fidOf(r.u32());
      // A number: Node writes at the descriptor's own position for any other.
      const offset = Number(r.u64());
      const data = r.bytes(r.u32());
      writable(fid);
      if (fid.fd === undefined) fail("EBADF");
      return [119, w.u32(writeSync(fid.fd, data, 0, data.length, offset))];
    },
    // Tclunk: fid[4]
    120: (r, w) => {
      const n = r.u32();
      close(fidOf(n));
      fids.delete(n);
      return [121, w];
    },
    // Tremove: fid[4]
    122: (r, w) => {
      const n = r.u32();
      const fid = fidOf(n);
      close(fid);
      fids.delete(n);
      writable(fid);
      if (stat(fid.path).isDirectory()) rmdirSync(B(fid.path));
      else unlinkSync(B(fid.path));
      return [123, w];
    },
  };

  // A rename, and every fid of this connection at or under its old path moved with it.
  function renamed(from, to) {
    renameSync(B(from), B(to));
    for (const fid of fids.values()) {
      if (fid.path === from || fid.path.startsWith(`${from}/`)) fid.path = to + fid.path.slice(from.length);
    }
  }

  socket.on("data", (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    while (pending.length >= 4 && pending.length >= pending.readUInt32LE(0)) {
      const size = pending.readUInt32LE(0);
      const message = pending.subarray(0, size);
      pending = pending.subarray(size);
      const type = message.readUInt8(4);
      const tag = message.readUInt16LE(5);
      const r = new Reader(message);
      r.at = 7;
      let answer;
      try {
        const handler = handlers[type] ?? fail("EOPNOTSUPP");
        const [rtype, w] = handler(r, new Writer());
        answer = w.done(rtype, tag);
      } catch (error) {
        answer = new Writer().u32(errno(error?.code)).done(7, tag);
      }
      socket.write(answer);
    }
  });
  socket.on("error", () => {});
  socket.on("close", () => {
    for (const fid of fids.values()) close(fid);
  });
}

const server = createServer(serve);
server.listen(process.argv[2], () => process.stdout.write(`${JSON.stringify({ listening: true })}\n`));
createInterface({ input: process.stdin }).on("line", (line) => {
  const asked = JSON.parse(line);
  if (asked.add) {
    shares.set(asked.add, { path: Buffer.from(asked.path, "utf8").toString("latin1"), readOnly: asked.readOnly === true });
    process.stdout.write(`${JSON.stringify({ added: asked.add })}\n`);
  } else if (asked.remove) {
    shares.delete(asked.remove);
    process.stdout.write(`${JSON.stringify({ removed: asked.remove })}\n`);
  }
}).on("close", () => process.exit(0));
