import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Failure, NUL_REFUSED, type Refusal } from "../src/files/answers.js";
import { expandUser, inside, keyInFolder, realpath, resolveInFolder } from "../src/files/paths.js";

let base: string;
let folder: string;

function refusal(call: () => unknown): Refusal {
  try {
    call();
  } catch (error) {
    if (error instanceof Failure) return error.refusal;
    throw error;
  }
  throw new Error("no refusal");
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "paths-")));
  folder = join(base, "folder");
  mkdirSync(join(base, "real", "inner"), { recursive: true });
  mkdirSync(join(folder, "sub"), { recursive: true });
  mkdirSync(join(base, "outside"));
  writeFileSync(join(folder, "a.txt"), "a");
  writeFileSync(join(base, "outside", "o.txt"), "o");
  symlinkSync("real/inner", join(base, "to-inner"));
  symlinkSync(join(base, "real"), join(base, "to-real"));
  symlinkSync("loop-b", join(base, "loop-a"));
  symlinkSync("loop-a", join(base, "loop-b"));
  symlinkSync("a.txt", join(folder, "link-in"));
  symlinkSync(join(base, "outside", "o.txt"), join(folder, "link-out"));
  symlinkSync("loop-y", join(folder, "loop-x"));
  symlinkSync("loop-x", join(folder, "loop-y"));
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("realpath, as CPython's non-strict os.path.realpath", () => {
  it("resolves .. against a symlink's target, not its text", () => {
    expect(realpath(`${base}/to-inner/..`)).toEqual({ path: `${base}/real`, loop: false });
  });

  it("follows an absolute target", () => {
    expect(realpath(`${base}/to-real/inner`).path).toBe(`${base}/real/inner`);
  });

  it("keeps a missing tail, and resolves .. past a missing name", () => {
    expect(realpath(`${base}/real/missing/x`).path).toBe(`${base}/real/missing/x`);
    expect(realpath(`${base}/missing/../real`).path).toBe(`${base}/real`);
  });

  it("collapses . and repeated slashes", () => {
    expect(realpath(`${base}//real/./inner/`).path).toBe(`${base}/real/inner`);
  });

  it("stays at / above the root", () => {
    expect(realpath("/..").path).toBe("/");
    expect(realpath("/../etc").path).toBe("/etc");
  });

  it("reports a loop", () => {
    expect(realpath(`${base}/loop-a/x`).loop).toBe(true);
  });

  it("leaves what a loop did not resolve as normpath tidies it, keeping exactly two leading slashes", () => {
    expect(realpath(`${base}/loop-a/x`)).toEqual({ path: `${base}/loop-a/x`, loop: true });
    expect(realpath(`${base}/loop-a//x`)).toEqual({ path: "/x", loop: true });
    expect(realpath(`${base}/loop-a///x`)).toEqual({ path: "//x", loop: true });
    expect(realpath(`${base}/loop-a////x`)).toEqual({ path: "/x", loop: true });
  });

  it("reuses what it learned of a link it crosses twice", () => {
    expect(realpath(`${base}/to-real/inner/../../to-real/inner`)).toEqual({ path: `${base}/real/inner`, loop: false });
  });
});

describe("expandUser, as CPython's os.path.expanduser", () => {
  it("expands a leading ~ and ~/ with the home folder, without its trailing slash", () => {
    expect(expandUser("~", "/home/u")).toBe("/home/u");
    expect(expandUser("~/x", "/home/u/")).toBe("/home/u/x");
    expect(expandUser("~", "/")).toBe("/");
  });

  it("expands ~name from the password file and leaves an unknown name as it is", () => {
    expect(expandUser("~root/x", "/home/u")).toBe("/root/x");
    expect(expandUser("~no-such-user-zz/x", "/home/u")).toBe("~no-such-user-zz/x");
  });

  it("leaves a ~ that does not lead the path", () => {
    expect(expandUser("a/~", "/home/u")).toBe("a/~");
  });
});

describe("inside", () => {
  it("compares whole components", () => {
    expect(inside("/f", "/f")).toBe(true);
    expect(inside("/f/a", "/f")).toBe(true);
    expect(inside("/f2/a", "/f")).toBe(false);
    expect(inside("/", "/f")).toBe(false);
  });
});

describe("resolveInFolder, as the cloud's resolve", () => {
  const home = "/home/tester";

  it("answers a path in the folder as its resolved key", () => {
    expect(resolveInFolder(folder, home, "a.txt")).toBe(`${folder}/a.txt`);
    expect(resolveInFolder(folder, home, "")).toBe(folder);
    expect(resolveInFolder(folder, home, ".")).toBe(folder);
    expect(resolveInFolder(folder, home, "sub/../a.txt")).toBe(`${folder}/a.txt`);
    expect(resolveInFolder(folder, home, "link-in")).toBe(`${folder}/a.txt`);
    expect(resolveInFolder(folder, home, `${folder}/sub/new/file`)).toBe(`${folder}/sub/new/file`);
  });

  it("refuses a path that leaves the folder, in the cloud's words", () => {
    expect(refusal(() => resolveInFolder(folder, home, "link-out"))).toEqual({
      type: "sandbox",
      message: `Path traversal blocked: 'link-out' resolves to '${base}/outside/o.txt' which is outside the workspace '${folder}'.`,
    });
    expect(refusal(() => resolveInFolder(folder, home, "../x")).type).toBe("sandbox");
    expect(refusal(() => resolveInFolder(folder, home, `${folder}2/x`)).type).toBe("sandbox");
    expect(refusal(() => resolveInFolder(folder, home, "~/x")).message).toContain("'/home/tester/x'");
  });

  it("refuses a NUL in the cloud's sentence, in a path and in a key", () => {
    expect(NUL_REFUSED).toBe("A path, a command or a search pattern cannot hold a NUL character");
    expect(refusal(() => resolveInFolder(folder, home, "a\0b"))).toEqual({ type: "value", message: NUL_REFUSED });
    expect(refusal(() => keyInFolder(folder, `${folder}/a\0b`))).toEqual({ type: "value", message: NUL_REFUSED });
  });

  it("answers a symlink loop as ELOOP, unless the path climbs back out of it", () => {
    expect(refusal(() => resolveInFolder(folder, home, "loop-x/f"))).toMatchObject({
      type: "os", code: "ELOOP",
    });
    expect(resolveInFolder(folder, home, "loop-x/../a.txt")).toBe(`${folder}/a.txt`);
  });

  it("reads repeated slashes as Python's Path does, so a loop is still a loop", () => {
    expect(refusal(() => resolveInFolder(folder, home, "loop-x//f"))).toMatchObject({
      type: "os", code: "ELOOP",
    });
    expect(resolveInFolder(folder, home, "loop-x//../a.txt")).toBe(`${folder}/a.txt`);
  });
});

describe("keyInFolder", () => {
  it("accepts a resolved path in the folder", () => {
    expect(keyInFolder(folder, `${folder}/a.txt`)).toBe(`${folder}/a.txt`);
    expect(keyInFolder(folder, folder)).toBe(folder);
    expect(keyInFolder(folder, `${folder}/sub/new`)).toBe(`${folder}/sub/new`);
  });

  it("refuses a key outside the folder, through a symlink, relative or unresolved", () => {
    for (const key of [`${base}/outside/o.txt`, `${folder}/link-in`, "a.txt", `${folder}/sub/../a.txt`, `${folder}/`]) {
      expect(refusal(() => keyInFolder(folder, key))).toEqual({
        type: "sandbox", message: `Not a path in this folder: '${key}'`,
      });
    }
  });

  it("refuses a NUL byte as Python does", () => {
    expect(refusal(() => keyInFolder(folder, `${folder}/a\0`)).type).toBe("value");
  });
});
