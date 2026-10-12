// The file host's calls relative to a folder it holds. Linux's make exactly the calls the file host made before
// they had an OS of their own, each on the path through the folder's /proc/self/fd entry; on an OS the file host
// is not built for yet, every one is refused.

import { closeSync, constants, mkdirSync, mkdtempSync, openSync, realpathSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { linuxFdPaths } from "../src/files/fd-paths-linux.js";
import { importedOn } from "./platforms.js";

// Each call of these the module made, by name and arguments.
const calls = vi.hoisted(() => ({ made: [] as unknown[][] }));
vi.mock("node:fs", async (importOriginal) => {
  const fs = await importOriginal<Record<string, unknown>>();
  const names = [
    "accessSync", "chmodSync", "copyFileSync", "linkSync", "lstatSync", "mkdirSync", "opendirSync", "openSync", "readdirSync",
    "readFileSync", "readlinkSync", "renameSync", "rmdirSync", "statSync", "unlinkSync",
  ];
  return {
    ...fs,
    ...Object.fromEntries(names.map((name) => [name, (...args: unknown[]) => {
      calls.made.push([name, ...args]);
      return (fs[name] as (...args: unknown[]) => unknown)(...args);
    }])),
  };
});

// What *call* returned, and the calls it made.
function made<T>(call: () => T): [T, unknown[][]] {
  calls.made = [];
  const result = call();
  return [result, calls.made];
}

// Linux's O_PATH, which Node does not name, written out: the folder held, never read.
const HOLD = 0o10000000 | constants.O_DIRECTORY | constants.O_NOFOLLOW;

let dir: string;
let held: number;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "fd-paths-")));
  writeFileSync(join(dir, "a"), "a");
  mkdirSync(join(dir, "sub"));
  held = linuxFdPaths.hold(dir);
});

afterEach(() => {
  closeSync(held);
  rmSync(dir, { recursive: true, force: true });
});

describe("Linux's FdPaths", () => {
  const at = (name?: string) => `/proc/self/fd/${held}${name === undefined ? "" : `/${name}`}`;

  it("holds a folder by O_PATH, never following a link, by its path or by its name in a folder it holds", () => {
    const [fd, calls] = made(() => linuxFdPaths.hold(dir));
    closeSync(fd);
    const [inner, more] = made(() => linuxFdPaths.hold({ dir: held, name: "sub" }));
    closeSync(inner);
    expect([calls, more]).toEqual([[["openSync", dir, HOLD]], [["openSync", at("sub"), HOLD]]]);
  });

  it("reaches a name in a held folder through its descriptor's entry, and a path as it is", () => {
    const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW;
    const [fd, opened] = made(() => linuxFdPaths.open({ dir: held, name: "b" }, flags, 0o600));
    closeSync(fd);
    const [read, plain] = made(() => linuxFdPaths.open(join(dir, "a"), constants.O_RDONLY));
    closeSync(read);
    expect([opened, plain]).toEqual([[["openSync", at("b"), flags, 0o600]], [["openSync", join(dir, "a"), constants.O_RDONLY]]]);
    const each: Array<[string, () => unknown, unknown[]]> = [
      ["lstatSync", () => linuxFdPaths.lstat({ dir: held, name: "a" }), [at("a"), { bigint: true }]],
      ["statSync", () => linuxFdPaths.stat({ dir: held, name: "a" }), [at("a")]],
      ["accessSync", () => linuxFdPaths.access({ dir: held, name: "a" }, constants.R_OK | constants.W_OK), [at("a"), constants.R_OK | constants.W_OK]],
      ["linkSync", () => linuxFdPaths.link({ dir: held, name: "a" }, { dir: held, name: "c" }), [at("a"), at("c")]],
      ["renameSync", () => linuxFdPaths.rename({ dir: held, name: "c" }, join(dir, "d")), [at("c"), join(dir, "d")]],
      ["copyFileSync", () => linuxFdPaths.copy({ dir: held, name: "d" }, join(dir, "e")), [at("d"), join(dir, "e")]],
      ["chmodSync", () => linuxFdPaths.chmod({ dir: held, name: "e" }, 0o600), [at("e"), 0o600]],
      ["unlinkSync", () => linuxFdPaths.unlink({ dir: held, name: "e" }), [at("e")]],
      ["mkdirSync", () => linuxFdPaths.mkdir({ dir: held, name: "f" }), [at("f")]],
      ["rmdirSync", () => linuxFdPaths.rmdir({ dir: held, name: "f" }), [at("f")]],
      // The held folder itself.
      ["chmodSync", () => linuxFdPaths.chmod({ dir: held }, 0o755), [at(), 0o755]],
    ];
    for (const [name, call, args] of each) expect(made(call)[1]).toEqual([[name, ...args]]);
    expect(linuxFdPaths.shown({ dir: held, name: "a" })).toBe(at("a"));
    expect(linuxFdPaths.shown(join(dir, "a"))).toBe(join(dir, "a"));
  });

  it("lists a held folder by its entry, its names as bytes", () => {
    const [listing, calls] = made(() => linuxFdPaths.listing(held));
    const names: string[] = [];
    for (let entry = listing.readSync(); entry !== null; entry = listing.readSync()) {
      expect(Buffer.isBuffer(entry.name)).toBe(true);
      names.push(entry.name.toString());
    }
    listing.closeSync();
    expect([calls, names.sort()]).toEqual([[["opendirSync", at(), { encoding: "buffer" }]], ["a", "sub"]]);
  });

  it("says where an open descriptor's file is now, and nothing for one removed since, or named as a removed one is", () => {
    const fd = openSync(join(dir, "a"), constants.O_RDONLY);
    try {
      expect(made(() => linuxFdPaths.pathOf(fd))).toEqual([join(dir, "a"), [["readlinkSync", `/proc/self/fd/${fd}`]]]);
      unlinkSync(join(dir, "a"));
      expect(linuxFdPaths.pathOf(fd)).toBeNull();
    } finally {
      closeSync(fd);
    }
    writeFileSync(join(dir, "x (deleted)"), "");
    const named = openSync(join(dir, "x (deleted)"), constants.O_RDONLY);
    try {
      expect(linuxFdPaths.pathOf(named)).toBeNull();
    } finally {
      closeSync(named);
    }
  });

  it("finds what a child would inherit by each descriptor's open flags, and none Node opened", () => {
    const fd = openSync(join(dir, "a"), constants.O_RDONLY);
    try {
      const [handed, calls] = made(() => linuxFdPaths.handed());
      expect(handed).not.toContain(fd);
      expect(handed.every((one) => one > 2)).toBe(true);
      expect(calls[0]).toEqual(["readdirSync", "/proc/self/fd"]);
      const read = calls.slice(1).map(([name, path, encoding]) => [name, String(path).replace(/[0-9]+$/, "<fd>"), encoding]);
      expect(new Set(read.map((one) => JSON.stringify(one)))).toEqual(new Set([JSON.stringify(["readFileSync", "/proc/self/fdinfo/<fd>", "utf8"])]));
      expect(calls.slice(1).map(([, path]) => String(path))).toContain(`/proc/self/fdinfo/${fd}`);
    } finally {
      closeSync(fd);
    }
  });
});

describe("the file host's FdPaths", () => {
  it("are Linux's on Linux", async () => {
    expect((await import("../src/files/fd-paths.js")).fdPaths).toBe(linuxFdPaths);
  });

  it("refuse every call, in words, on a system the file host is not built for yet", async () => {
    const { fdPaths } = await importedOn("darwin", () => import("../src/files/fd-paths.js"));
    const words = "The file tools are not built for this computer's system yet";
    expect(() => fdPaths.hold(dir)).toThrow(new Error(words));
    expect(() => fdPaths.open({ dir: held, name: "a" }, constants.O_RDONLY)).toThrow(new Error(words));
    expect(() => fdPaths.handed()).toThrow(new Error(words));
  });
});
