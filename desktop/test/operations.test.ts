import { execFileSync } from "node:child_process";
import {
  chmodSync, linkSync, mkdirSync, mkdtempSync, type ReadPosition, readdirSync, readFileSync, realpathSync, rmSync,
  statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_MESSAGE_CHARS, MAX_NAMES, MAX_PAYLOAD_BYTES, MAX_READ_BYTES, MAX_WRITE_BYTES, READ_TOO_LARGE, WRITE_TOO_LARGE,
} from "../src/files/answers.js";
import { type Context, perform } from "../src/files/operations.js";
import { inFolderRefusal } from "../src/files/protect.js";

// The file helper's reads come back at most this long: some filesystems answer less than asked.
const reads = vi.hoisted(() => ({ cap: Number.POSITIVE_INFINITY }));
vi.mock("node:fs", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs")>();
  const readSync = (
    fd: number, buffer: NodeJS.ArrayBufferView, offset: number, length: number, position: ReadPosition | null,
  ) => fs.readSync(fd, buffer, offset, Math.min(length, reads.cap), position);
  return { ...fs, readSync, default: { ...fs, readSync } };
});

let base: string;
let folder: string;
let context: Context;

const run = (kind: string, args: Record<string, unknown>) => perform(kind, args, context, new AbortController().signal);
const b64 = (text: string | Buffer) => Buffer.from(text).toString("base64");

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "operations-")));
  folder = join(base, "folder");
  mkdirSync(join(folder, "sub"), { recursive: true });
  mkdirSync(join(base, "outside"));
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  writeFileSync(join(base, "outside", "o.txt"), "outside\n");
  symlinkSync("a.txt", join(folder, "link-in"));
  context = { folder, home: "/home/tester", env: { PATH: "/usr/bin:/bin", HOME: "/home/tester" } };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("resolve and check_write", () => {
  it("answer through the folder's rules", async () => {
    expect(await run("resolve", { path: "sub/../a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(await run("resolve", { path: "../outside/o.txt" })).toMatchObject({ error: { type: "sandbox" } });
    expect(await run("check_write", { path: ".git/config" })).toEqual({ ok: inFolderRefusal(".git/config") });
    expect(await run("check_write", { path: "a.txt" })).toEqual({ ok: null });
  });
});

describe("stat", () => {
  it("answers exactly is_dir, size and mtime", async () => {
    const answer = await run("stat", { key: `${folder}/a.txt` });
    expect(Object.keys((answer as { ok: object }).ok).sort()).toEqual(["is_dir", "mtime", "size"]);
    expect(answer).toMatchObject({ ok: { is_dir: false, size: 6 } });
    const { mtime } = (answer as { ok: { mtime: number } }).ok;
    expect(mtime).toBeCloseTo(statSync(join(folder, "a.txt")).mtimeMs / 1000, 3);
    expect(await run("stat", { key: `${folder}/sub` })).toMatchObject({ ok: { is_dir: true } });
  });

  it.each([
    ["@1700000000.123456789", 1700000000.1234567],
    ["@-1.000000001", -1.000000001],
    ["@-1.5", -1.5],
    ["@0.000000001", 1e-9],
    // These three tell the formula apart from its near misses: dropping the
    // borrow for a negative time, dividing by 1e9, or dividing the whole count.
    ["@-1.758663714", -1.7586637139999999],
    ["@-0.128129134", -0.12812913399999992],
    ["@1700000000.987654321", 1700000000.9876542],
  ])("answers the mtime of %s to the last bit, as CPython's st_mtime", async (time, expected) => {
    execFileSync("touch", ["-d", time, join(folder, "a.txt")]);
    const answer = (await run("stat", { key: `${folder}/a.txt` })) as { ok: { mtime: number } };
    expect(answer.ok.mtime).toBe(expected);
  });

  it("answers null for anything it cannot stat", async () => {
    for (const key of [`${folder}/missing`, `${folder}/a\0`, `${base}/outside/o.txt`, `${folder}/link-in`]) {
      expect(await run("stat", { key })).toEqual({ ok: null });
    }
  });
});

describe("read", () => {
  it("answers the file's bytes, or its first max_bytes", async () => {
    expect(await run("read", { key: `${folder}/a.txt`, max_bytes: null })).toEqual({ ok: b64("alpha\n") });
    expect(await run("read", { key: `${folder}/a.txt`, max_bytes: 3 })).toEqual({ ok: b64("alp") });
    expect(await run("read", { key: `${folder}/a.txt`, max_bytes: 0 })).toEqual({ ok: "" });
  });

  it("answers a file of up to 50 MiB whole, and fails a larger one unless only its head is asked for", async () => {
    const most = Buffer.alloc(MAX_READ_BYTES, 120);
    most.write("the end", MAX_READ_BYTES - 7);
    writeFileSync(join(folder, "most.bin"), most);
    const whole = await run("read", { key: `${folder}/most.bin`, max_bytes: null });
    expect(Buffer.from((whole as { ok: string }).ok, "base64").equals(most)).toBe(true);
    writeFileSync(join(folder, "huge.bin"), Buffer.alloc(MAX_READ_BYTES + 1, 120));
    expect(await run("read", { key: `${folder}/huge.bin`, max_bytes: null })).toEqual({
      error: { type: "os", code: "EFBIG", message: READ_TOO_LARGE },
    });
    const head = await run("read", { key: `${folder}/huge.bin`, max_bytes: 8192 });
    expect(Buffer.from((head as { ok: string }).ok, "base64").length).toBe(8192);
  });

  it("keeps only the bytes each short read returned", async () => {
    // Every read of a 1 MiB piece comes back 64 KiB; a copy that size is not from Buffer's pool.
    const size = 3 * 64 * 1024 + 5000;
    writeFileSync(join(folder, "short.bin"), Buffer.alloc(size, 122));
    reads.cap = 64 * 1024;
    const concat = vi.spyOn(Buffer, "concat");
    try {
      const answer = await run("read", { key: `${folder}/short.bin`, max_bytes: null });
      expect(Buffer.from((answer as { ok: string }).ok, "base64").equals(Buffer.alloc(size, 122))).toBe(true);
      const [pieces] = concat.mock.calls[0] as [Buffer[]];
      expect(pieces.map((piece) => piece.buffer.byteLength)).toEqual([65536, 65536, 65536, 5000]);
    } finally {
      reads.cap = Number.POSITIVE_INFINITY;
      concat.mockRestore();
    }
  });

  it("answers a file of exactly 1 MiB", async () => {
    writeFileSync(join(folder, "exact.bin"), Buffer.alloc(MAX_PAYLOAD_BYTES, 121));
    const answer = await run("read", { key: `${folder}/exact.bin`, max_bytes: null });
    expect(Buffer.from((answer as { ok: string }).ok, "base64").equals(Buffer.alloc(MAX_PAYLOAD_BYTES, 121))).toBe(true);
  });

  it("answers OS errors in Python's words", async () => {
    expect(await run("read", { key: `${folder}/sub`, max_bytes: null })).toEqual({
      error: { type: "os", code: "EISDIR", message: `Is a directory: '${folder}/sub'` },
    });
    expect(await run("read", { key: `${folder}/missing`, max_bytes: null })).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${folder}/missing'` },
    });
    writeFileSync(join(folder, "locked.txt"), "x");
    chmodSync(join(folder, "locked.txt"), 0o000);
    expect(await run("read", { key: `${folder}/locked.txt`, max_bytes: null })).toMatchObject({
      error: { type: "os", code: "EACCES" },
    });
  });

  it("refuses a FIFO at once instead of blocking", async () => {
    execFileSync("mkfifo", [join(folder, "pipe")]);
    const started = Date.now();
    expect(await run("read", { key: `${folder}/pipe`, max_bytes: null })).toEqual({
      error: { type: "os", code: "EINVAL", message: `Not a regular file: '${folder}/pipe'` },
    });
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it("reads a file with another hard link", async () => {
    linkSync(join(base, "outside", "o.txt"), join(folder, "hard.txt"));
    expect(await run("read", { key: `${folder}/hard.txt`, max_bytes: null })).toEqual({ ok: b64("outside\n") });
  });

  it("refuses a key that is not a resolved path in the folder", async () => {
    for (const key of [`${base}/outside/o.txt`, `${folder}/link-in`]) {
      expect(await run("read", { key, max_bytes: null })).toEqual({
        error: { type: "sandbox", message: `Not a path in this folder: '${key}'` },
      });
    }
  });

  it("refuses a max_bytes that is not a whole number", async () => {
    for (const max_bytes of [-1, 1.5, "3"]) {
      expect(await run("read", { key: `${folder}/a.txt`, max_bytes })).toMatchObject({ error: { type: "value" } });
    }
  });
});

describe("write", () => {
  it("writes a new file, making its folders, and leaves no temporary file", async () => {
    expect(await run("write", { key: `${folder}/new/deep/n.txt`, data: b64("new\n") })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "new", "deep", "n.txt"), "utf8")).toBe("new\n");
    expect(readdirSync(join(folder, "new", "deep"))).toEqual(["n.txt"]);
  });

  it("replaces a file and keeps its mode", async () => {
    writeFileSync(join(folder, "run.sh"), "#!/bin/sh\n");
    chmodSync(join(folder, "run.sh"), 0o755);
    expect(await run("write", { key: `${folder}/run.sh`, data: b64("#!/bin/sh\necho\n") })).toEqual({ ok: null });
    expect(statSync(join(folder, "run.sh")).mode & 0o777).toBe(0o755);
    expect(readFileSync(join(folder, "run.sh"), "utf8")).toBe("#!/bin/sh\necho\n");
  });

  it("answers a parent that is a file as Python's makedirs does", async () => {
    expect(await run("write", { key: `${folder}/a.txt/x`, data: b64("x") })).toEqual({
      error: { type: "os", code: "EEXIST", message: `File exists: '${folder}/a.txt'` },
    });
    expect(await run("write", { key: `${folder}/a.txt/sub/x`, data: b64("x") })).toEqual({
      error: { type: "os", code: "ENOTDIR", message: `Not a directory: '${folder}/a.txt/sub'` },
    });
    expect(await run("write", { key: `${folder}/a.txt/s/d/e`, data: b64("x") })).toEqual({
      error: { type: "os", code: "ENOTDIR", message: `Not a directory: '${folder}/a.txt/s'` },
    });
  });

  it("replaces a file this user cannot read, as the cloud does", async () => {
    writeFileSync(join(folder, "locked.txt"), "old");
    chmodSync(join(folder, "locked.txt"), 0o000);
    expect(await run("write", { key: `${folder}/locked.txt`, data: b64("new") })).toEqual({ ok: null });
    chmodSync(join(folder, "locked.txt"), 0o600);
    expect(readFileSync(join(folder, "locked.txt"), "utf8")).toBe("new");
  });

  it("does not write through another hard link", async () => {
    linkSync(join(base, "outside", "o.txt"), join(folder, "hard.txt"));
    expect(await run("write", { key: `${folder}/hard.txt`, data: b64("changed") })).toEqual({
      error: {
        type: "os", code: "EMLINK",
        message: `File has more than one hard link, so it is not changed: '${folder}/hard.txt'`,
      },
    });
    expect(readFileSync(join(base, "outside", "o.txt"), "utf8")).toBe("outside\n");
  });

  it("refuses a directory, a FIFO, a protected name and a key outside the folder", async () => {
    execFileSync("mkfifo", [join(folder, "pipe")]);
    expect(await run("write", { key: `${folder}/sub`, data: b64("x") })).toMatchObject({ error: { code: "EISDIR" } });
    expect(await run("write", { key: `${folder}/pipe`, data: b64("x") })).toMatchObject({ error: { code: "EINVAL" } });
    expect(await run("write", { key: `${folder}/.git/config`, data: b64("x") })).toEqual({
      error: { type: "sandbox", message: inFolderRefusal(`${folder}/.git/config`) },
    });
    expect(await run("write", { key: `${base}/outside/o.txt`, data: b64("x") })).toMatchObject({ error: { type: "sandbox" } });
    expect(readFileSync(join(base, "outside", "o.txt"), "utf8")).toBe("outside\n");
  });

  it("writes exactly 1 MiB", async () => {
    const exact = Buffer.alloc(MAX_PAYLOAD_BYTES, 122);
    expect(await run("write", { key: `${folder}/exact.bin`, data: b64(exact) })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "exact.bin")).equals(exact)).toBe(true);
  });

  it("writes 50 MiB, the most it takes, checking its base64 without growing a stack", async () => {
    const most = Buffer.alloc(MAX_WRITE_BYTES, 120);
    most.write("the end", MAX_WRITE_BYTES - 7);
    expect(await run("write", { key: `${folder}/most.bin`, data: b64(most) })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "most.bin")).equals(most)).toBe(true);
    // A pattern with a repeated group overflows the regex engine's stack on this much text.
    const broken = `${b64(most).slice(0, -4)}AA=A`;
    expect(await run("write", { key: `${folder}/most.bin`, data: broken })).toMatchObject({ error: { type: "value" } });
  });

  it("names the key, not the temporary file, when it cannot create one", async () => {
    chmodSync(folder, 0o555);
    try {
      expect(await run("write", { key: `${folder}/new.txt`, data: b64("x") })).toEqual({
        error: { type: "os", code: "EACCES", message: `Permission denied: '${folder}/new.txt'` },
      });
    } finally {
      chmodSync(folder, 0o755);
    }
  });

  it("answers data far over 50 MiB as too large, without checking or decoding it", async () => {
    // Not base64 either: only the size of the text, checked first, answers it so.
    expect(await run("write", { key: `${folder}/x.txt`, data: "@".repeat(75_000_000) })).toEqual({
      error: { type: "os", code: "EFBIG", message: WRITE_TOO_LARGE },
    });
  });

  it("refuses data that is not standard padded base64, or over 50 MiB", async () => {
    for (const data of ["@@", "YQ", "YQ=\n", "Y-8_", "YQ==YQ==", "Y===", "====", "YWé=", "😀=="]) {
      expect(await run("write", { key: `${folder}/x.txt`, data })).toMatchObject({ error: { type: "value" } });
    }
    // As long, encoded, as 50 MiB: only its decoded size is over.
    const big = Buffer.alloc(MAX_WRITE_BYTES + 1).toString("base64");
    expect(await run("write", { key: `${folder}/x.txt`, data: big })).toEqual({
      error: { type: "os", code: "EFBIG", message: WRITE_TOO_LARGE },
    });
    expect(readdirSync(folder)).not.toContain("x.txt");
  });
});

describe("delete", () => {
  it("removes a file", async () => {
    expect(await run("delete", { key: `${folder}/a.txt` })).toEqual({ ok: null });
    expect(readdirSync(folder)).not.toContain("a.txt");
  });

  it("refuses a missing file, a directory and the folder itself", async () => {
    expect(await run("delete", { key: `${folder}/missing` })).toMatchObject({ error: { code: "ENOENT" } });
    expect(await run("delete", { key: `${folder}/sub` })).toEqual({
      error: { type: "os", code: "EISDIR", message: `Is a directory: '${folder}/sub'` },
    });
    expect(await run("delete", { key: folder })).toEqual({
      error: { type: "os", code: "EISDIR", message: `Is a directory: '${folder}'` },
    });
  });

  it("refuses a protected name", async () => {
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", "pre-commit"), "x");
    expect(await run("delete", { key: `${folder}/.git/hooks/pre-commit` })).toMatchObject({ error: { type: "sandbox" } });
  });
});

describe("list_dir", () => {
  it("answers the names in a folder, hidden ones too", async () => {
    writeFileSync(join(folder, ".hidden"), "");
    const answer = (await run("list_dir", { key: folder })) as { ok: string[] };
    expect(answer.ok.sort()).toEqual([".hidden", "a.txt", "link-in", "sub"]);
  });

  it("answers OS errors in Python's words", async () => {
    expect(await run("list_dir", { key: `${folder}/a.txt` })).toEqual({
      error: { type: "os", code: "ENOTDIR", message: `Not a directory: '${folder}/a.txt'` },
    });
    expect(await run("list_dir", { key: `${folder}/missing` })).toMatchObject({ error: { code: "ENOENT" } });
  });

  it("answers at most MAX_NAMES names", async () => {
    mkdirSync(join(folder, "many"));
    for (let i = 0; i < MAX_NAMES + 5; i++) writeFileSync(join(folder, "many", `f${i}`), "");
    expect(((await run("list_dir", { key: `${folder}/many` })) as { ok: string[] }).ok).toHaveLength(MAX_NAMES);
  });

  it("answers a listing too large for one message as too_large", async () => {
    mkdirSync(join(folder, "long"));
    const name = "n".repeat(200);
    for (let i = 0; i < MAX_MESSAGE_CHARS / 200; i++) writeFileSync(join(folder, "long", `${name}${i}`), "");
    expect(await run("list_dir", { key: `${folder}/long` })).toEqual({
      error: { type: "too_large", message: "The result of list_dir is too large" },
    });
  });
});

describe("any kind", () => {
  it("answers a kind this computer does not do", async () => {
    expect(await run("run", { command: "true" })).toEqual({
      error: { type: "unsupported", message: "This computer cannot do 'run' yet" },
    });
  });

  it("answers the names every object has as kinds this computer does not do", async () => {
    for (const kind of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(await run(kind, {})).toEqual({
        error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` },
      });
    }
  });

  it("refuses arguments of the wrong type", async () => {
    expect(await run("read", { key: 1, max_bytes: null })).toMatchObject({ error: { type: "value" } });
    expect(await run("resolve", {})).toMatchObject({ error: { type: "value" } });
  });
});
