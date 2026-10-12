// The file host's calls relative to a folder it holds, on macOS: fsat's (native/fsat, built by its build.sh) against
// a real APFS folder, each as Node's own call would answer it; and the helper's look at its folder before each
// request. On a Mac alone: Linux's are fd-paths.test.ts's.

import { spawn, spawnSync } from "node:child_process";
import {
  closeSync, constants, linkSync, lstatSync, mkdirSync, mkdtempSync, openSync, readFileSync, readSync, realpathSync, renameSync, rmdirSync,
  rmSync, statSync, symlinkSync, unlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FOLDER_UNAVAILABLE } from "../src/files/answers.js";
import { darwinFdPaths as paths } from "../src/files/fd-paths-darwin.js";
import { FLAGS } from "../src/files/flags.js";
import { fsat } from "../src/files/fsat.js";

const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));
const CONSTANTS = fileURLToPath(new URL("../native/fsat/build/constants", import.meta.url));

let base: string;
let dir: string;
let held: number;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "fd-paths-darwin-")));
  dir = join(base, "folder");
  mkdirSync(join(dir, "sub"), { recursive: true });
  writeFileSync(join(dir, "a"), "a");
  held = paths.hold(dir);
});

afterEach(() => {
  closeSync(held);
  rmSync(base, { recursive: true, force: true });
});

const contents = (fd: number): string => {
  const buffer = Buffer.alloc(64);
  try {
    return buffer.subarray(0, readSync(fd, buffer, 0, 64, 0)).toString();
  } finally {
    closeSync(fd);
  }
};
const code = (call: () => unknown): string | undefined => {
  try {
    call();
    return undefined;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code;
  }
};

describe.runIf(process.platform === "darwin")("macOS's FdPaths", () => {
  it("are fsat's numbers, Darwin's headers' own, name for name", () => {
    const headers = JSON.parse(spawnSync(CONSTANTS, { encoding: "utf8" }).stdout) as Record<string, number>;
    expect(fsat().constants).toEqual(headers);
    expect(FLAGS.NOFOLLOW).toBe(constants.O_NOFOLLOW | headers.O_NOFOLLOW_ANY!);
  });

  it("hold a folder by its descriptor, and never through a link at its name, nor anything but a folder", () => {
    symlinkSync("sub", join(dir, "link"));
    closeSync(paths.hold({ dir: held, name: "sub" }));
    // A link refused as either: its folder's caller takes both for no folder (land.ts, into).
    expect(["ELOOP", "ENOTDIR"]).toContain(code(() => paths.hold({ dir: held, name: "link" })));
    expect(code(() => paths.hold({ dir: held, name: "a" }))).toBe("ENOTDIR");
    expect(["ELOOP", "ENOTDIR"]).toContain(code(() => paths.hold(join(dir, "link"))));
  });

  it("reach a name in a held folder through its descriptor, whatever its path leads to now", () => {
    renameSync(dir, join(base, "moved"));
    mkdirSync(dir);
    writeFileSync(join(dir, "a"), "another");
    expect(contents(paths.open({ dir: held, name: "a" }, constants.O_RDONLY))).toBe("a");
    expect(paths.pathOf(held)).toBe(join(base, "moved"));
  });

  it("make each call on a name in a held folder, as Node's call on its path does", () => {
    const at = (name: string) => ({ dir: held, name });
    closeSync(paths.open(at("b"), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o640));
    expect(statSync(join(dir, "b")).mode & 0o777).toBe(0o640 & ~process.umask());
    expect(code(() => paths.open(at("b"), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600))).toBe("EEXIST");
    paths.access(at("a"), constants.R_OK | constants.W_OK);
    expect(code(() => paths.access(at("missing"), constants.R_OK))).toBe("ENOENT");
    paths.link(at("a"), at("c"));
    expect(statSync(join(dir, "c")).ino).toBe(statSync(join(dir, "a")).ino);
    paths.rename(at("c"), join(dir, "d"));
    paths.copy(at("d"), join(dir, "e"));
    expect(readFileSync(join(dir, "e"), "utf8")).toBe("a");
    paths.copy(at("d"), at("f"));
    expect([readFileSync(join(dir, "f"), "utf8"), statSync(join(dir, "f")).mode & 0o777]).toEqual(["a", statSync(join(dir, "a")).mode & 0o777]);
    paths.chmod(at("e"), 0o600);
    expect(statSync(join(dir, "e")).mode & 0o777).toBe(0o600);
    paths.unlink(at("e"));
    paths.mkdir(at("g"));
    expect(statSync(join(dir, "g")).isDirectory()).toBe(true);
    paths.rmdir(at("g"));
    expect(code(() => statSync(join(dir, "g")))).toBe("ENOENT");
    // The held folder itself.
    paths.chmod({ dir: held }, 0o751);
    expect(statSync(dir).mode & 0o777).toBe(0o751);
    paths.chmod({ dir: held }, 0o755);
    expect(paths.shown(at("a"))).toBe(join(dir, "a"));
    expect(paths.shown(join(dir, "a"))).toBe(join(dir, "a"));
  });

  it("give a link's own stat, or what it leads to, as Node's lstat and stat do, each number as Node's", () => {
    symlinkSync("a", join(dir, "link"));
    for (const name of ["a", "sub", "link"]) {
      const mine = paths.lstat({ dir: held, name });
      const node = lstatSync(join(dir, name), { bigint: true });
      for (const field of ["dev", "ino", "mode", "nlink", "uid", "gid", "size", "atimeNs", "mtimeNs", "ctimeNs", "birthtimeNs"] as const) {
        expect(mine[field], `${name} ${field}`).toBe(node[field]);
      }
      expect([mine.isFile(), mine.isDirectory(), mine.isSymbolicLink()]).toEqual([node.isFile(), node.isDirectory(), node.isSymbolicLink()]);
      const followed = paths.stat({ dir: held, name });
      const nodeFollowed = statSync(join(dir, name));
      for (const field of ["dev", "ino", "mode", "size", "mtimeMs", "ctimeMs"] as const) expect(followed[field], `${name} ${field}`).toBe(nodeFollowed[field]);
    }
    expect(paths.lstat({ dir: held }).ino).toBe(lstatSync(dir, { bigint: true }).ino);
  });

  it("list a held folder's entries as bytes with their kinds, from its first each time, leaving the folder held", () => {
    symlinkSync("a", join(dir, "link"));
    const read = () => {
      const listing = paths.listing(held);
      const seen: string[] = [];
      for (let entry = listing.readSync(); entry !== null; entry = listing.readSync()) {
        expect(Buffer.isBuffer(entry.name)).toBe(true);
        seen.push(`${entry.name.toString()}:${entry.isDirectory() ? "d" : entry.isSymbolicLink() ? "l" : entry.isFile() ? "f" : "?"}`);
      }
      listing.closeSync();
      return seen.sort();
    };
    expect(read()).toEqual(["a:f", "link:l", "sub:d"]);
    expect(read()).toEqual(["a:f", "link:l", "sub:d"]);
    expect(paths.lstat({ dir: held, name: "a" }).isFile()).toBe(true);
  });

  it("open a path with no link on the way to it, where O_NOFOLLOW alone follows one above its last name", () => {
    symlinkSync(dir, join(base, "via"));
    closeSync(openSync(join(base, "via", "a"), constants.O_RDONLY | constants.O_NOFOLLOW));
    expect(code(() => openSync(join(base, "via", "a"), constants.O_RDONLY | FLAGS.NOFOLLOW))).toBe("ELOOP");
    expect(code(() => paths.open(join(base, "via", "a"), constants.O_RDONLY | FLAGS.NOFOLLOW))).toBe("ELOOP");
    closeSync(paths.open(join(dir, "a"), constants.O_RDONLY | FLAGS.NOFOLLOW));
  });

  it("rename over nothing with RENAME_EXCL, and over a name that is there without", () => {
    writeFileSync(join(dir, "b"), "b");
    expect(code(() => fsat().renameatx(held, "a", held, "b", fsat().constants.RENAME_EXCL))).toBe("EEXIST");
    expect(readFileSync(join(dir, "b"), "utf8")).toBe("b");
    fsat().renameatx(held, "a", held, "c", fsat().constants.RENAME_EXCL);
    fsat().renameatx(held, "c", held, "b", 0);
    expect(readFileSync(join(dir, "b"), "utf8")).toBe("a");
  });

  it("refuse, before any call, a name that is no name in the folder, and fail as Node's calls fail", () => {
    for (const name of ["..", "sub/a", "", "a\0b"]) expect(code(() => fsat().openat(held, name, constants.O_RDONLY, 0)), JSON.stringify(name)).toBe("EINVAL");
    expect(code(() => fsat().openat(fsat().constants.AT_FDCWD, "relative", constants.O_RDONLY, 0))).toBe("EINVAL");
    let failure: NodeJS.ErrnoException | undefined;
    try {
      fsat().openat(held, "missing", constants.O_RDONLY, 0);
    } catch (error) {
      failure = error as NodeJS.ErrnoException;
    }
    expect(failure).toMatchObject({ code: "ENOENT", errno: -2, syscall: "openat", path: "missing", message: "ENOENT: no such file or directory, openat 'missing'" });
  });

  it("say where an open file is now, and nothing for one whose name in the folder is gone, though another name of it is left", () => {
    const fd = openSync(join(dir, "a"), constants.O_RDONLY);
    const folder = paths.hold({ dir: held, name: "sub" });
    try {
      expect(paths.pathOf(fd)).toBe(join(dir, "a"));
      linkSync(join(dir, "a"), join(base, "outside"));
      unlinkSync(join(dir, "a"));
      expect(paths.pathOf(fd)).not.toBe(join(dir, "a"));
      unlinkSync(join(base, "outside"));
      expect(paths.pathOf(fd)).toBeNull();
      expect(paths.pathOf(folder)).toBe(join(dir, "sub"));
      rmdirSync(join(dir, "sub"));
      expect(paths.pathOf(folder)).toBeNull();
    } finally {
      closeSync(fd);
      closeSync(folder);
    }
  });

  it("find no descriptor Node opened among what a child would inherit, and tell a closed one from an open one", () => {
    const fd = openSync(join(dir, "a"), constants.O_RDONLY);
    expect(fsat().cloexec(fd)).toBe(true);
    const handed = paths.handed();
    expect(handed).not.toContain(fd);
    expect(handed.every((one) => one > 2)).toBe(true);
    closeSync(fd);
    expect(fsat().cloexec(fd)).toBeNull();
  });

  it("measure whether a folder this user may pass through but not list can be held for search alone", () => {
    const search = fsat().constants.O_SEARCH;
    mkdirSync(join(dir, "closed"));
    writeFileSync(join(dir, "closed", "x"), "x");
    spawnSync("/bin/chmod", ["311", join(dir, "closed")]);
    const said: Record<string, string> = { O_SEARCH: String(search) };
    const tried = (what: string, call: () => unknown) => {
      try {
        const out = call();
        said[what] = `ok ${typeof out === "number" || typeof out === "string" ? out : ""}`.trim();
      } catch (error) {
        said[what] = (error as NodeJS.ErrnoException).code ?? String(error);
      }
    };
    let fd = -1;
    tried("hold O_RDONLY", () => closeSync(paths.hold({ dir: held, name: "closed" })));
    if (search !== undefined) {
      tried("open O_SEARCH", () => (fd = fsat().openat(held, "closed", search | constants.O_NOFOLLOW, 0)));
      if (fd >= 0) {
        tried("openat in it", () => closeSync(fsat().openat(fd, "x", constants.O_RDONLY, 0)));
        tried("fstatat in it", () => fsat().fstatat(fd, "x", 0).size);
        tried("fstat of it", () => fsat().fstatat(fd, null, 0).ino);
        tried("fchmod of it", () => fsat().fchmodat(fd, null, 0o311, 0));
        tried("F_GETPATH of it", () => fsat().getpath(fd));
        tried("mkdirat in it", () => fsat().mkdirat(fd, "made", 0o755));
        tried("renameatx in it", () => fsat().renameatx(fd, "x", fd, "y", 0));
        closeSync(fd);
      }
    }
    spawnSync("/bin/chmod", ["755", join(dir, "closed")]);
    console.log(`O_SEARCH: ${JSON.stringify(said)}`);
    expect(said["hold O_RDONLY"]).toBe("EACCES");
  });
});

describe.runIf(process.platform === "darwin")("the file helper on macOS", () => {
  it("looks at its folder before each request, and answers one in another folder made at its path as unavailable", async () => {
    const { dev, ino } = statSync(dir);
    const child = spawn(process.execPath, [HELPER], {
      env: { SUROGATE_FOLDER: dir, SUROGATE_FOLDER_IS: `${dev}:${ino}`, HOME: base, PATH: "/usr/bin:/bin" }, stdio: ["pipe", "pipe", "inherit"],
    });
    const heard: unknown[] = [];
    const lines = createInterface({ input: child.stdout });
    const next = () => new Promise<unknown>((resolve) => lines.once("line", (line) => resolve(JSON.parse(line))));
    try {
      expect(await next()).toEqual({ ready: true });
      const ask = async (id: string) => {
        child.stdin.write(`${JSON.stringify({ id, kind: "read", args: { key: join(dir, "a"), max_bytes: null } })}\n`);
        heard.push(await next());
      };
      await ask("1");
      renameSync(dir, join(base, "moved"));
      mkdirSync(dir);
      writeFileSync(join(dir, "a"), "another");
      await ask("2");
      rmSync(dir, { recursive: true });
      renameSync(join(base, "moved"), dir);
      await ask("3");
      expect(heard).toEqual([
        { id: "1", outcome: { ok: Buffer.from("a").toString("base64") } }, { id: "2", outcome: FOLDER_UNAVAILABLE },
        { id: "3", outcome: { ok: Buffer.from("a").toString("base64") } },
      ]);
    } finally {
      child.kill("SIGKILL");
    }
  });
});
