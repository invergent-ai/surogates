// What write and read do when a system call fails after the file is open. The
// calls are real unless a test switches one to fail.

import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type Context, perform } from "../src/files/operations.js";

const failing = vi.hoisted(() => ({ close: false, fchmod: false, rename: false }));

vi.mock("node:fs", async (original) => {
  const fs = await original<typeof import("node:fs")>();
  const failure = (message: string, code: string, errno: number, syscall: string) =>
    Object.assign(new Error(`${code}: ${message}, ${syscall}`), { code, errno, syscall });
  return {
    ...fs,
    // The descriptor is closed for real; the error is the deferred one a network filesystem reports.
    closeSync: (fd: number) => {
      fs.closeSync(fd);
      if (failing.close) throw failure("i/o error", "EIO", -5, "close");
    },
    fchmodSync: (fd: number, mode: number) => {
      if (failing.fchmod) throw failure("operation not permitted", "EPERM", -1, "fchmod");
      fs.fchmodSync(fd, mode);
    },
    renameSync: (from: string, to: string) => {
      if (failing.rename) throw failure("cross-device link not permitted", "EXDEV", -18, "rename");
      fs.renameSync(from, to);
    },
  };
});

let base: string;
let folder: string;
let context: Context;

const run = (kind: string, args: Record<string, unknown>) => perform(kind, args, context, new AbortController().signal);
const b64 = (text: string) => Buffer.from(text).toString("base64");

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "write-failures-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  context = { folder, home: "/home/tester", env: {} };
});

afterEach(() => {
  failing.close = failing.fchmod = failing.rename = false;
  rmSync(base, { recursive: true, force: true });
});

describe("a write that fails after the file is open", () => {
  it("goes on when the mode cannot be kept, as the cloud does", async () => {
    failing.fchmod = true;
    expect(await run("write", { key: `${folder}/a.txt`, data: b64("new\n") })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("new\n");
    expect(readdirSync(folder)).toEqual(["a.txt"]);
  });

  it("removes its temporary file when the rename fails, and leaves the old file", async () => {
    failing.rename = true;
    expect(await run("write", { key: `${folder}/a.txt`, data: b64("new\n") })).toEqual({
      error: { type: "os", code: "EXDEV", message: `Invalid cross-device link: '${folder}/a.txt'` },
    });
    expect(readdirSync(folder)).toEqual(["a.txt"]);
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("alpha\n");
  });

  it("answers a deferred error on close as an OS error, and removes its temporary file", async () => {
    failing.close = true;
    expect(await run("write", { key: `${folder}/a.txt`, data: b64("new\n") })).toEqual({
      error: { type: "os", code: "EIO", message: `Input/output error: '${folder}/a.txt'` },
    });
    expect(readdirSync(folder)).toEqual(["a.txt"]);
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("alpha\n");
  });
});

describe("a read that fails on close", () => {
  it("answers a deferred error on close as an OS error", async () => {
    failing.close = true;
    expect(await run("read", { key: `${folder}/a.txt`, max_bytes: null })).toEqual({
      error: { type: "os", code: "EIO", message: `Input/output error: '${folder}/a.txt'` },
    });
  });
});
