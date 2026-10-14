// The walk on Windows, on the runner's own NTFS (native/ntat/ntat.c, src/files/fd-paths-win32.ts): each name opened
// relative to the folder before it, every reparse point refused, every name Windows aliases refused before it is
// opened and every alias after, and each handle a descriptor of Node's own. Mirrors Go's os.Root tests on Windows
// (src/os/root_test.go: the junction and symlink cases; src/internal/syscall/windows/at_windows_test.go). It needs
// dist/native/ntat.node (native/ntat/build.ps1) and runs on Windows alone.

import { execFileSync } from "node:child_process";
import {
  closeSync, fstatSync, fsyncSync, ftruncateSync, linkSync, mkdirSync, mkdtempSync, readFileSync, readSync, realpathSync, rmSync, statSync,
  symlinkSync, writeFileSync, writeSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFUSED, TAKEN } from "./windows-names.js";

const WINDOWS = process.platform === "win32";

async function fresh() {
  vi.resetModules();
  const [walk, { ntat }, { constants }, names, { perform }] = await Promise.all([
    import("../src/files/fd-paths-win32.js"), import("../src/files/ntat.js"), import("node:fs"), import("../src/files/names-win32.js"),
    import("../src/files/operations.js"),
  ]);
  return { ...walk, fdPaths: walk.win32FdPaths, ntat: ntat(), fs: constants, names, perform };
}

let base: string;
let host: string;
let outside: string;

beforeEach(() => {
  if (!WINDOWS) return;
  // Its long names, as Windows gives them: the runner's temp folder is named by a short name.
  base = realpathSync.native(mkdtempSync(join(tmpdir(), "ntat-")));
  host = join(base, "folder");
  outside = join(base, "outside");
  mkdirSync(host);
  mkdirSync(outside);
  writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
});

afterEach(() => {
  if (WINDOWS) rmSync(base, { recursive: true, force: true });
});

describe.runIf(WINDOWS)("the walk on Windows", { timeout: 60_000 }, () => {
  it("names a folder in the guest's form, /mnt/<drive>/..., and back", async () => {
    const { guestPathOf, hostPathOf } = await fresh();
    expect(guestPathOf("\\\\?\\C:\\Users\\Ana\\shop")).toBe("/mnt/c/Users/Ana/shop");
    expect(guestPathOf("C:\\")).toBe("/mnt/c");
    expect(hostPathOf("/mnt/c/Users/Ana/shop")).toBe("C:\\Users\\Ana\\shop");
    expect(hostPathOf("/mnt/c")).toBe("C:\\");
    expect([hostPathOf("/home/ana"), hostPathOf("/mnt/cd/x"), guestPathOf("\\\\?\\UNC\\server\\share")]).toEqual([null, null, null]);
  });

  it("hands Node its own descriptors, which Node's fs reads, writes, stats and closes once, and leaves no handle behind", async () => {
    const { fdPaths, guestPathOf, ntat, fs } = await fresh();
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const fd = fdPaths.open(`${root}/a`, fs.O_RDWR | fs.O_CREAT | fs.O_EXCL, 0o666);
    expect(writeSync(fd, "hello world")).toBe(11);
    ftruncateSync(fd, 5);
    fsyncSync(fd);
    expect(fstatSync(fd).size).toBe(5);
    closeSync(fd);
    expect(readFileSync(join(host, "a"), "utf8")).toBe("hello");
    expect(() => closeSync(fd)).toThrow(/EBADF/);
    const before = ntat.handles();
    const buffer = Buffer.alloc(16);
    for (let i = 0; i < 1_000; i += 1) {
      const read = fdPaths.open(`${root}/a`, fs.O_RDONLY);
      readSync(read, buffer, 0, 16, 0);
      fstatSync(read);
      closeSync(read);
      fdPaths.lstat(`${root}/a`);
      fdPaths.readdir(root);
      fdPaths.mkdir(`${root}/d`);
      fdPaths.rmdir(`${root}/d`);
    }
    // The process's own threads may open a handle or two meanwhile; a thousand operations' worth would be thousands.
    expect(ntat.handles() - before).toBeLessThan(10);
  });

  it("makes each of Node's calls on a folder's file, through the folder's handle", async () => {
    const { fdPaths, guestPathOf, fs } = await fresh();
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    fdPaths.mkdir(`${root}/sub`);
    closeSync(fdPaths.open(`${root}/sub/a`, fs.O_WRONLY | fs.O_CREAT | fs.O_EXCL));
    expect(fdPaths.exists(`${root}/sub/a`)).toBe(true);
    expect(fdPaths.exists(`${root}/sub/b`)).toBe(false);
    expect(fdPaths.stat(`${root}/sub`).isDirectory()).toBe(true);
    expect(fdPaths.stat(`${root}/sub/a`, { bigint: true }).isFile()).toBe(true);
    expect(fdPaths.readdir(`${root}/sub`)).toEqual(["a"]);
    fdPaths.access(`${root}/sub/a`, fs.R_OK | fs.W_OK);
    fdPaths.chmod(`${root}/sub/a`, 0o444);
    expect(statSync(join(host, "sub", "a")).mode & 0o222).toBe(0);
    fdPaths.chmod(`${root}/sub/a`, 0o666);
    fdPaths.link(`${root}/sub/a`, `${root}/sub/hard`);
    expect(statSync(join(host, "sub", "a")).nlink).toBe(2);
    expect(Number(fdPaths.lstat(`${root}/sub/hard`).nlink)).toBe(2);
    fdPaths.rename(`${root}/sub/hard`, `${root}/b`);
    fdPaths.copy(`${root}/b`, `${root}/c`);
    expect(statSync(join(host, "c")).nlink).toBe(1);
    fdPaths.unlink(`${root}/b`);
    expect(fdPaths.exists(`${root}/b`)).toBe(false);
    const held = fdPaths.hold(`${root}/sub`);
    try {
      const listing = fdPaths.listing(held);
      const names: string[] = [];
      for (let entry = listing.readSync(); entry !== null; entry = listing.readSync()) names.push(`${entry.name.toString("utf8")}:${entry.isFile()}`);
      listing.closeSync();
      expect(names).toEqual(["a:true"]);
      const fd = fdPaths.open({ dir: held, name: "a" }, fs.O_RDONLY);
      expect(fdPaths.pathOf(fd)).toBe(`${root}/sub/a`);
      expect(fdPaths.shown({ dir: held, name: "a" })).toBe(`${root}/sub/a`);
      fdPaths.unlink({ dir: held, name: "a" });
      expect(fdPaths.pathOf(fd)).toBeNull();
      closeSync(fd);
    } finally {
      closeSync(held);
    }
    fdPaths.rmdir(`${root}/sub`);
    expect(Array.isArray(fdPaths.handed())).toBe(true);
    // A folder above the bound one is a folder and nothing more; anywhere else is not there.
    expect(fdPaths.lstat("/mnt").isDirectory()).toBe(true);
    expect(() => fdPaths.open(guestPathOf(join(outside, "only-outside"))!, fs.O_RDONLY)).toThrow(expect.objectContaining({ code: "ENOENT" }));
  });

  it("refuses a junction, a directory symlink and a file symlink at any name, and opens nothing through one", async () => {
    const { fdPaths, guestPathOf, fs, ntat } = await fresh();
    symlinkSync(outside, join(host, "junction"), "junction");
    symlinkSync(outside, join(host, "dirlink"), "dir");
    symlinkSync(join(outside, "only-outside"), join(host, "filelink"), "file");
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    for (const link of ["junction", "dirlink"]) {
      expect(fdPaths.lstat(`${root}/${link}`).isSymbolicLink(), link).toBe(true);
      for (const call of [
        () => fdPaths.open(`${root}/${link}/only-outside`, fs.O_RDONLY), () => fdPaths.readdir(`${root}/${link}`), () => fdPaths.hold(`${root}/${link}`),
        () => fdPaths.stat(`${root}/${link}`), () => fdPaths.open(`${root}/${link}/new`, fs.O_WRONLY | fs.O_CREAT), () => fdPaths.unlink(`${root}/${link}/only-outside`),
        () => fdPaths.mkdir(`${root}/${link}/made`), () => fdPaths.rename(`${root}/${link}/only-outside`, `${root}/stolen`),
      ]) {
        expect(call, link).toThrow(expect.objectContaining({ code: "ELOOP", refused: "link" }));
      }
    }
    expect(fdPaths.lstat(`${root}/filelink`).isSymbolicLink()).toBe(true);
    expect(() => fdPaths.open(`${root}/filelink`, fs.O_RDONLY)).toThrow(expect.objectContaining({ code: "ELOOP" }));
    expect(() => fdPaths.readlink(`${root}/filelink`)).toThrow(/do not follow on Windows/);
    // The links' tags, as the listing gives them, mark each one a link.
    const held = fdPaths.hold(root);
    const listing = fdPaths.listing(held);
    const links: string[] = [];
    for (let entry = listing.readSync(); entry !== null; entry = listing.readSync()) if (entry.isSymbolicLink()) links.push(entry.name.toString());
    listing.closeSync();
    closeSync(held);
    expect(links.sort()).toEqual(["dirlink", "filelink", "junction"]);
    expect(readFileSync(join(outside, "only-outside"), "utf8")).toBe("OUTSIDE\n");
    expect(ntat.constants.IO_REPARSE_TAG_MOUNT_POINT >>> 0).toBe(0xa0000003);
  });

  it("binds no folder that is a link itself, nor one whose path goes through a link", async () => {
    const { fdPaths, guestPathOf } = await fresh();
    symlinkSync(outside, join(base, "linked"), "junction");
    expect(() => fdPaths.bind([guestPathOf(join(base, "linked"))!])).toThrow(expect.objectContaining({ code: "ELOOP" }));
    mkdirSync(join(outside, "inner"));
    expect(() => fdPaths.bind([guestPathOf(join(base, "linked", "inner"))!])).toThrow(/is not where its path leads/);
    expect(() => fdPaths.bind(["/home/ana/folder"])).toThrow(/\/mnt\/<drive>/);
  });

  it("refuses each name Windows aliases before anything is opened, in the file tools' words, and ntat refuses each itself", async () => {
    const { fdPaths, guestPathOf, fs, ntat, names } = await fresh();
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const named = REFUSED.filter((name) => name !== "" && !name.includes("/") && name !== "." && name !== "..");
    for (const name of named) {
      expect(() => fdPaths.open(`${root}/${name}`, fs.O_WRONLY | fs.O_CREAT), JSON.stringify(name)).toThrow(names.nameRefusal(name));
    }
    // ntat's own check, beneath the file host's: the same answer for every name, and nothing made.
    const dir = ntat.openRoot(`\\\\?\\${host}`);
    try {
      const k = ntat.constants;
      for (const name of REFUSED.filter((name) => name.length <= 300)) {
        expect(() => ntat.openAt(dir, name, k.FILE_GENERIC_WRITE, k.FILE_OPEN_IF, 0, k.FILE_SHARE_READ, 0, false), JSON.stringify(name))
          .toThrow(expect.objectContaining({ refused: "name" }));
      }
      for (const name of TAKEN) closeSync(ntat.openAt(dir, name, k.FILE_GENERIC_WRITE, k.FILE_OPEN_IF, 0, k.FILE_SHARE_READ, k.FILE_ATTRIBUTE_NORMAL, false));
    } finally {
      closeSync(dir);
    }
    expect(fdPaths.readdir(root).sort()).toEqual([...TAKEN].sort());
  });

  it("refuses a short name, by its form and by the long name of what it opens, so GIT~1 never reaches .git", async () => {
    const { fdPaths, guestPathOf, fs, ntat } = await fresh();
    mkdirSync(join(host, ".git", "hooks"), { recursive: true });
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const git = fdPaths.hold(`${root}/.git`);
    const short = ntat.query(git).shortName;
    closeSync(git);
    console.log(`short name of .git on the runner's volume: ${JSON.stringify(short)}`);
    if (short !== null) expect(() => fdPaths.open(`${root}/${short}/hooks/x`, fs.O_WRONLY | fs.O_CREAT)).toThrow(`This name cannot be used on Windows: ${short}`);
    // A short name without the form NTFS gives one, set by an administrator: the long-name check alone refuses it.
    execFileSync("fsutil", ["file", "setshortname", join(host, ".git"), "GITDIR"]);
    expect(() => fdPaths.open(`${root}/GITDIR/hooks/x`, fs.O_WRONLY | fs.O_CREAT)).toThrow("This name cannot be used on Windows: GITDIR");
    const dir = ntat.openRoot(`\\\\?\\${host}`);
    try {
      expect(() => ntat.openAt(dir, "GITDIR", ntat.constants.FILE_TRAVERSE, ntat.constants.FILE_OPEN, ntat.constants.FILE_DIRECTORY_FILE, 7, 0, false))
        .toThrow(expect.objectContaining({ refused: "alias" }));
      // Its long name, in another ASCII case, is the same file and opens.
      closeSync(ntat.openAt(dir, ".GIT", ntat.constants.FILE_TRAVERSE, ntat.constants.FILE_OPEN, ntat.constants.FILE_DIRECTORY_FILE, 7, 0, false));
    } finally {
      closeSync(dir);
    }
    expect(statSync(join(host, ".git", "hooks")).isDirectory()).toBe(true);
    expect(fdPaths.readdir(`${root}/.git/hooks`)).toEqual([]);
  });

  it("says what a handle is: its tag, links, identity, long and short names, and whether its folder tells case apart", async () => {
    const { ntat, identityFrom } = await fresh();
    const dir = ntat.openRoot(`\\\\?\\${host}`);
    try {
      const said = ntat.query(dir);
      expect([said.tag, said.directory, said.links, said.longName, said.id.length]).toEqual([null, true, 1, "folder", 16]);
      console.log(`the runner's temp folder: case sensitive ${JSON.stringify(said.caseSensitive)}`);
      expect(identityFrom(said)).toEqual(identityFrom(ntat.query(dir)));
      expect(identityFrom(said).ino).toBeLessThan(2 ** 52);
    } finally {
      closeSync(dir);
    }
  });

  it("gives one identity for one folder by two paths, a subst drive's and its real one, binds it by the real one alone, and another for the folder made in its place", async () => {
    const { ntat, identityFrom, fdPaths, guestPathOf } = await fresh();
    const used = new Set(execFileSync("cmd", ["/c", "subst"], { encoding: "utf8" }).split(/\r?\n/).map((line) => line.slice(0, 1)));
    const letter = [..."ZYXWVUTSRQ"].find((candidate) => !used.has(candidate) && !(() => {
      try {
        statSync(`${candidate}:\\`);
        return true;
      } catch {
        return false;
      }
    })())!;
    execFileSync("subst", [`${letter}:`, host]);
    try {
      const [real, substituted] = [ntat.openRoot(`\\\\?\\${host}`), ntat.openRoot(`\\\\?\\${letter}:\\`)];
      expect(identityFrom(ntat.query(substituted))).toEqual(identityFrom(ntat.query(real)));
      closeSync(real);
      closeSync(substituted);
      expect(() => fdPaths.bind([`/mnt/${letter.toLowerCase()}`])).toThrow(/is not where its path leads/);
    } finally {
      execFileSync("subst", [`${letter}:`, "/d"]);
    }
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const first = fdPaths.lstat(root);
    rmSync(host, { recursive: true });
    mkdirSync(host);
    // The bound folder's path holds another now: none is answered for it.
    expect(() => fdPaths.lstat(root)).toThrow(expect.objectContaining({ code: "ENOENT" }));
    const again = await fresh();
    again.fdPaths.bind([root]);
    const second = again.fdPaths.lstat(root);
    expect(`${second.dev}:${second.ino}`).not.toBe(`${first.dev}:${first.ino}`);
  });

  it("reads this boot's number from the registry, and this process's user's SID", async () => {
    const { ntat } = await fresh();
    const reg = execFileSync("reg", ["query", "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters", "/v", "BootId"], { encoding: "utf8" });
    expect(ntat.bootId()).toBe(Number.parseInt(/BootId\s+REG_DWORD\s+0x([0-9a-f]+)/i.exec(reg)![1]!, 16));
    const { bootId } = await import("../src/binding/boot-id.js");
    expect(bootId()).toBe(String(ntat.bootId()));
    const sid = /S-1-[0-9-]+/.exec(execFileSync("whoami", ["/user", "/fo", "csv", "/nh"], { encoding: "utf8" }))![0];
    expect(ntat.userSid()).toBe(sid);
  });

  it("refuses a hard link's write, as every OS's file tools do", async () => {
    const { guestPathOf, perform, fdPaths } = await fresh();
    writeFileSync(join(host, "one"), "x");
    linkSync(join(host, "one"), join(host, "two"));
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const outcome = await perform("write", { key: `${root}/one`, data: Buffer.from("y").toString("base64") }, { folder: root, home: root, env: {} }, new AbortController().signal);
    expect(outcome).toMatchObject({ error: { code: "EMLINK" } });
    expect(readFileSync(join(host, "two"), "utf8")).toBe("x");
  });
});
