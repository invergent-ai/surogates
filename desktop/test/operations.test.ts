import { execFileSync } from "node:child_process";
import { once } from "node:events";
import {
  type BigIntStats, chmodSync, linkSync, mkdirSync, mkdtempSync, type ReadPosition, readdirSync, readFileSync, realpathSync, rmSync,
  statSync, symlinkSync, truncateSync, utimesSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Worker } from "node:worker_threads";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_MESSAGE_CHARS, MAX_NAMES, MAX_PAYLOAD_BYTES, MAX_READ_BYTES, MAX_WALK_FILES, MAX_WRITE_BYTES, READ_TOO_LARGE,
  WALK_MARGIN_NS, WRITE_TOO_LARGE,
} from "../src/files/answers.js";
import { BAD_PAGE, BAD_WALK, type Context, perform, revisionOf } from "../src/files/operations.js";
import { inFolderRefusal } from "../src/files/protect.js";

// The file helper's reads come back at most this long: some filesystems answer less than asked. And how many it made.
// Each of `next`, while there are any, caps one read instead; a 0 finds the file's end there.
const reads = vi.hoisted(() => ({ cap: Number.POSITIVE_INFINITY, calls: 0, next: [] as number[] }));
// Run once as the file helper's next write begins: another writer, changing a file meanwhile.
const meanwhile = vi.hoisted(() => ({ run: null as (() => void) | null }));
vi.mock("node:fs", async (importOriginal) => {
  const fs = await importOriginal<typeof import("node:fs")>();
  const readSync = (
    fd: number, buffer: NodeJS.ArrayBufferView, offset: number, length: number, position: ReadPosition | null,
  ) => {
    reads.calls += 1;
    const cap = reads.next.shift() ?? reads.cap;
    return cap === 0 ? 0 : fs.readSync(fd, buffer, offset, Math.min(length, cap), position);
  };
  const writeSync = (fd: number, buffer: NodeJS.ArrayBufferView, offset?: number) => {
    const run = meanwhile.run;
    meanwhile.run = null;
    run?.();
    return fs.writeSync(fd, buffer, offset);
  };
  return { ...fs, readSync, writeSync, default: { ...fs, readSync, writeSync } };
});

let base: string;
let folder: string;
let context: Context;

const run = (kind: string, args: Record<string, unknown>) => perform(kind, args, context, new AbortController().signal);
const b64 = (text: string | Buffer) => Buffer.from(text).toString("base64");
const revision = async (key: string) => ((await run("stat", { key })) as { ok: { revision: string } }).ok.revision;

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
  it("answers exactly is_dir, size, mtime and revision", async () => {
    const answer = await run("stat", { key: `${folder}/a.txt` });
    expect(Object.keys((answer as { ok: object }).ok).sort()).toEqual(["is_dir", "mtime", "revision", "size"]);
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

  it("answers the revision as the cloud makes it: dev, inode, size, and mtime and ctime in nanoseconds", async () => {
    const key = join(folder, "a.txt");
    const shown = execFileSync("stat", ["-c", "%d:%i:%s:%.9Y:%.9Z", key], { env: { ...process.env, LC_ALL: "C" } });
    expect(await revision(key)).toBe(shown.toString().trim().replaceAll(".", ""));
  });

  it("answers another revision once the file changes, even with its size and mtime put back", async () => {
    const key = join(folder, "a.txt");
    utimesSync(key, 1_700_000_000, 1_700_000_000);
    const before = await revision(key);
    // Past the filesystem's timestamp tick, so the ctime moves.
    await new Promise((resolve) => setTimeout(resolve, 20));
    writeFileSync(key, "ALPHA\n");
    utimesSync(key, 1_700_000_000, 1_700_000_000);
    expect(statSync(key, { bigint: true }).mtimeNs).toBe(1_700_000_000_000_000_000n);
    expect(await revision(key)).not.toBe(before);
  });

  it("answers an inode at or above 2^63 unsigned, as Python's st_ino is", () => {
    // Node fills BigIntStats from a signed array: mergerfs hashes and SMB file ids read as negative there.
    const st = { dev: 66313n, ino: -2n, size: 6n, mtimeNs: -1n, ctimeNs: 7n } as BigIntStats;
    expect(revisionOf(st)).toBe("66313:18446744073709551614:6:-1:7");
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

  it("answers a file over 50 MiB asked for whole as too large from its size, before reading a byte of it", async () => {
    // Sparse: 2 GiB costs nothing to make.
    writeFileSync(join(folder, "huge.bin"), "");
    truncateSync(join(folder, "huge.bin"), 2 * 1024 ** 3);
    reads.calls = 0;
    for (const max_bytes of [null, MAX_READ_BYTES + 1]) {
      expect(await run("read", { key: `${folder}/huge.bin`, max_bytes })).toEqual({
        error: { type: "os", code: "EFBIG", message: READ_TOO_LARGE },
      });
    }
    expect(reads.calls).toBe(0);
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

describe("read_lines", () => {
  const page = (key: string, more: Record<string, unknown> = {}) =>
    run("read_lines", { key, encoding: "utf-8", offset: 1, limit: 2000, max_bytes: 200_000, ...more });
  const answer = (data: string | Buffer, total_lines: number) => ({ ok: { data: b64(data), total_lines } });
  // As Python's utf-32-le and utf-32-be codecs encode it.
  const utf32 = (text: string, little: boolean) => {
    const points = [...text].map((character) => character.codePointAt(0) ?? 0);
    const out = Buffer.alloc(points.length * 4);
    points.forEach((point, i) => (little ? out.writeUInt32LE(point, i * 4) : out.writeUInt32BE(point, i * 4)));
    return out;
  };
  const ENCODED: [string, (text: string) => Buffer][] = [
    ["utf-16-le", (text) => Buffer.from(text, "utf16le")],
    ["utf-16-be", (text) => Buffer.from(text, "utf16le").swap16()],
    ["utf-32-le", (text) => utf32(text, true)],
    ["utf-32-be", (text) => utf32(text, false)],
  ];

  it("answers the bytes of a window of whole lines, and how many lines the file has", async () => {
    writeFileSync(join(folder, "mixed.txt"), "one\ntwo\r\nthree\rfour");
    expect(await page(`${folder}/mixed.txt`)).toEqual(answer("one\ntwo\r\nthree\rfour", 4));
    expect(await page(`${folder}/mixed.txt`, { offset: 2, limit: 2 })).toEqual(answer("two\r\nthree\r", 4));
    expect(await page(`${folder}/mixed.txt`, { offset: 4 })).toEqual(answer("four", 4));
  });

  it("selects lines as a Python slice does: none past the end, and a limit below one too", async () => {
    writeFileSync(join(folder, "three.txt"), "one\ntwo\nthree\n");
    for (const [more, data] of [
      [{ offset: 4 }, ""],
      [{ limit: 0 }, ""],
      [{ offset: 2, limit: -1 }, ""],
      [{ limit: -1 }, "one\ntwo\n"],
      [{ limit: -2 }, "one\n"],
      [{ limit: -3 }, ""],
      [{ offset: 2, limit: -4 }, ""],
      [{ offset: 2, limit: -2 }, "two\n"],
    ] as const) {
      expect(await page(`${folder}/three.txt`, more)).toEqual(answer(data, 3));
    }
  });

  it("answers the first max_bytes of a first line longer than that, and only whole lines otherwise", async () => {
    writeFileSync(join(folder, "long.txt"), `${"x".repeat(100)}\nshort\n`);
    expect(await page(`${folder}/long.txt`, { max_bytes: 10 })).toEqual(answer("x".repeat(10), 2));
    writeFileSync(join(folder, "two.txt"), "ab\ncd\n");
    expect(await page(`${folder}/two.txt`, { max_bytes: 4 })).toEqual(answer("ab\n", 2));
    expect(await page(`${folder}/two.txt`, { max_bytes: 0 })).toEqual(answer("", 2));
  });

  it("finds line ends as code units of UTF-16 and UTF-32, in either byte order", async () => {
    // 上 (U+4E0A) and 不 (U+4E0D) have units that hold 0x0A and 0x0D, and end no line.
    for (const [encoding, encode] of ENCODED) {
      writeFileSync(join(folder, "u.txt"), encode("\ufeff上\r\n不\nlast"));
      expect(await page(`${folder}/u.txt`, { encoding })).toEqual(answer(encode("\ufeff上\r\n不\nlast"), 3));
      expect(await page(`${folder}/u.txt`, { encoding, offset: 2, limit: 1 })).toEqual(answer(encode("不\n"), 3));
    }
  });

  it("starts a utf-8-sig file's first line after its BOM", async () => {
    writeFileSync(join(folder, "sig.txt"), "\ufeffone\ntwo\n");
    expect(await page(`${folder}/sig.txt`, { encoding: "utf-8-sig", limit: 1 })).toEqual(answer("one\n", 2));
    expect(await page(`${folder}/sig.txt`, { limit: 1 })).toEqual(answer("\ufeffone\n", 2));
    writeFileSync(join(folder, "bom.txt"), "\ufeff");
    expect(await page(`${folder}/bom.txt`, { encoding: "utf-8-sig" })).toEqual(answer("", 0));
  });

  it("ends one line at a CR LF across two pieces of the file", async () => {
    writeFileSync(join(folder, "edge.txt"), `${"x".repeat(1024 * 1024 - 1)}\r\ny\r\n`);
    expect(await page(`${folder}/edge.txt`, { offset: 2 })).toEqual(answer("y\r\n", 2));
    writeFileSync(join(folder, "edge16.txt"), Buffer.from(`${"x".repeat(512 * 1024 - 1)}\r\ny\r\n`, "utf16le"));
    expect(await page(`${folder}/edge16.txt`, { encoding: "utf-16-le", offset: 2 })).toEqual(
      answer(Buffer.from("y\r\n", "utf16le"), 2),
    );
  });

  it("pages alike when every read comes back shorter than a code unit", async () => {
    const encode = (text: string) => utf32(text, true);
    writeFileSync(join(folder, "u.txt"), encode("one\r\ntwo\rthree\n"));
    reads.cap = 3;
    try {
      expect(await page(`${folder}/u.txt`, { encoding: "utf-32-le", offset: 2 })).toEqual(answer(encode("two\rthree\n"), 3));
    } finally {
      reads.cap = Number.POSITIVE_INFINITY;
    }
  });

  it("tops a piece up to a whole code unit when a growing file's end cuts one", async () => {
    const lines = Buffer.from(Array.from({ length: 11 }, (_, number) => `line ${number}\n`).join(""), "utf16le");
    writeFileSync(join(folder, "growing.txt"), lines);
    // The first read stops at byte 9, mid-unit, and the next finds the file's end there, for a moment.
    reads.next = [9, 0];
    try {
      expect(await page(`${folder}/growing.txt`, { encoding: "utf-16-le" })).toEqual(answer(lines, 11));
    } finally {
      reads.next = [];
    }
  });

  it("answers a page of 1 MiB, inside one message", async () => {
    writeFileSync(join(folder, "line.txt"), "x".repeat(MAX_PAYLOAD_BYTES + 10));
    expect(await page(`${folder}/line.txt`, { max_bytes: MAX_PAYLOAD_BYTES })).toEqual(
      answer(Buffer.alloc(MAX_PAYLOAD_BYTES, "x"), 1),
    );
  });

  it("answers a file over 50 MiB as too large from its size, before reading a byte of it", async () => {
    writeFileSync(join(folder, "huge.log"), Buffer.alloc(MAX_READ_BYTES + 1, 10));
    reads.calls = 0;
    expect(await page(`${folder}/huge.log`)).toEqual({ error: { type: "os", code: "EFBIG", message: READ_TOO_LARGE } });
    expect(reads.calls).toBe(0);
  });

  it("answers OS errors in Python's words, and a FIFO at once", async () => {
    expect(await page(`${folder}/sub`)).toEqual({
      error: { type: "os", code: "EISDIR", message: `Is a directory: '${folder}/sub'` },
    });
    expect(await page(`${folder}/missing`)).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${folder}/missing'` },
    });
    execFileSync("mkfifo", [join(folder, "pipe")]);
    expect(await page(`${folder}/pipe`)).toEqual({
      error: { type: "os", code: "EINVAL", message: `Not a regular file: '${folder}/pipe'` },
    });
  });

  it("refuses a key that is not a resolved path in the folder, and arguments it does not take", async () => {
    for (const key of [`${base}/outside/o.txt`, `${folder}/link-in`]) {
      expect(await page(key)).toEqual({ error: { type: "sandbox", message: `Not a path in this folder: '${key}'` } });
    }
    for (const more of [
      { encoding: "latin-1" }, { encoding: null }, { offset: 0 }, { offset: 1.5 }, { limit: "3" }, { limit: true },
      { max_bytes: -1 }, { max_bytes: MAX_PAYLOAD_BYTES + 1 },
    ]) {
      expect(await page(`${folder}/a.txt`, more)).toEqual({ error: { type: "value", message: BAD_PAGE } });
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

  it("writes only while the file is at the revision the write expects, and makes nothing otherwise", async () => {
    const key = `${folder}/a.txt`;
    const seen = await revision(key);
    expect(await run("write", { key, data: b64("two\n"), expected_revision: seen })).toEqual({ ok: null });
    expect(readFileSync(key, "utf8")).toBe("two\n");
    // As the cloud's reference laptop words it (tests/fake_laptop.py's CONFLICT).
    expect(await run("write", { key, data: b64("three\n"), expected_revision: seen })).toEqual({
      error: {
        type: "conflict",
        message: `${key} changed on this computer after it was read, so it was not written. Read it again, then make the change again`,
      },
    });
    expect(readFileSync(key, "utf8")).toBe("two\n");
    for (const expected of [seen, 5]) {
      expect(await run("write", { key: `${folder}/gone/n.txt`, data: b64("x"), expected_revision: expected })).toMatchObject({
        error: { type: "conflict" },
      });
    }
    expect(readdirSync(folder).sort()).toEqual(["a.txt", "link-in", "sub"]);
  });

  it("does not land over a change made while its temp file was written", async () => {
    const key = `${folder}/a.txt`;
    const seen = await revision(key);
    // A chat bound to a folder nested in this one has its own helper, and an editor can save too.
    meanwhile.run = () => writeFileSync(key, "theirs\n");
    try {
      expect(await run("write", { key, data: b64("ours\n"), expected_revision: seen })).toMatchObject({
        error: { type: "conflict" },
      });
    } finally {
      meanwhile.run = null;
    }
    expect(readFileSync(key, "utf8")).toBe("theirs\n");
    expect(readdirSync(folder).sort()).toEqual(["a.txt", "link-in", "sub"]);
  });

  it("writes whatever is there when the write expects no revision", async () => {
    expect(await run("write", { key: `${folder}/a.txt`, data: b64("x"), expected_revision: null })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("x");
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

describe("walk", () => {
  type Walked = { ok: { files: Array<[string, number]>; truncated: boolean; cursor: string } };
  const walk = async (args: Record<string, unknown> = {}) =>
    (await run("walk", { key: folder, skip: [], skip_top: [], skip_hidden: false, since: null, ...args })) as Walked;
  const listed = (walked: Walked) => [...walked.ok.files].sort();

  it("lists the regular files under the key with their sizes, follows no link and enters no skipped folder", async () => {
    writeFileSync(join(folder, "sub", "b.md"), "beta!");
    mkdirSync(join(folder, "node_modules", "x"), { recursive: true });
    writeFileSync(join(folder, "node_modules", "x", "i.js"), "");
    symlinkSync(join(base, "outside"), join(folder, "out"));
    const walked = await walk({ skip: ["node_modules"] });
    expect(listed(walked)).toEqual([["a.txt", 6], ["sub/b.md", 5]]);
    expect(walked.ok.truncated).toBe(false);
    expect(walked.ok.cursor).toMatch(/^\d+$/);
    expect(listed(await walk({ key: join(folder, "sub") }))).toEqual([["b.md", 5]]);
  });

  it("since a cursor, lists only the files changed after it, by mtime or ctime", async () => {
    writeFileSync(join(folder, "old.txt"), "o");
    // Past the cursor's margin, which covers a filesystem's coarser clock.
    await new Promise((resolve) => setTimeout(resolve, Number(WALK_MARGIN_NS / 1_000_000n) + 100));
    const first = await walk();
    writeFileSync(join(folder, "new.txt"), "n");
    // An mtime set back keeps a new ctime: changed all the same.
    utimesSync(join(folder, "a.txt"), new Date(0), new Date(0));
    expect(listed(await walk({ since: first.ok.cursor }))).toEqual([["a.txt", 6], ["new.txt", 1]]);
  });

  it("enters no folder the tree hides: a skipped name, one at the top, and a hidden one", async () => {
    for (const name of ["src", ".cache", ".github", "_whiteboard", "sub/_whiteboard", "node_modules"]) {
      mkdirSync(join(folder, name), { recursive: true });
      writeFileSync(join(folder, name, "f.txt"), "x");
    }
    const walked = await walk({ skip: ["node_modules"], skip_top: ["_whiteboard"], skip_hidden: true });
    expect(listed(walked)).toEqual([[".github/f.txt", 1], ["a.txt", 6], ["src/f.txt", 1], ["sub/_whiteboard/f.txt", 1]]);
  });

  it("leaves out a name that is not UTF-8, and lists its decoded twin once", async () => {
    writeFileSync(Buffer.concat([Buffer.from(`${folder}/`), Buffer.from([0x6e, 0xff])]), "bytes");
    writeFileSync(join(folder, "n\ufffd"), "t");
    expect(listed(await walk())).toEqual([["a.txt", 6], ["n\ufffd", 1]]);
  });

  it("never enters a folder swapped for a link while it walks", async () => {
    mkdirSync(join(folder, "aaa"));
    writeFileSync(join(folder, "aaa", "in.txt"), "in");
    // As a command in the VM writing the folder could: the folder swapped for a link outside, and back.
    const state = new Int32Array(new SharedArrayBuffer(8)); // [stop, swaps]
    const swapper = new Worker(`
      const { renameSync, symlinkSync, unlinkSync } = require("node:fs");
      const { workerData: { aaa, real, outside, state } } = require("node:worker_threads");
      while (Atomics.load(state, 0) === 0) {
        try {
          renameSync(aaa, real); symlinkSync(outside, aaa); unlinkSync(aaa); renameSync(real, aaa);
          Atomics.add(state, 1, 1);
        } catch {}
      }
    `, { eval: true, workerData: { aaa: join(folder, "aaa"), real: join(folder, "aaa.real"), outside: join(base, "outside"), state } });
    const leaked = new Set<string>();
    try {
      for (const deadline = Date.now() + 2_000; Date.now() < deadline;) {
        for (const [path] of (await walk()).ok.files) if (path === "aaa/o.txt") leaked.add(path);
      }
    } finally {
      Atomics.store(state, 0, 1);
      await once(swapper, "exit");
    }
    expect(Atomics.load(state, 1)).toBeGreaterThan(100);
    expect([...leaked]).toEqual([]);
  });

  it("stops at its cap and says it did", async () => {
    mkdirSync(join(folder, "many"));
    for (let i = 0; i < MAX_WALK_FILES; i++) writeFileSync(join(folder, "many", String(i)), "");
    const walked = await walk();
    expect(walked.ok.files).toHaveLength(MAX_WALK_FILES);
    expect(walked.ok.truncated).toBe(true);
  });

  it("fails as its folder does, and refuses a key or arguments it cannot take", async () => {
    expect(await walk({ key: `${folder}/a.txt` })).toMatchObject({ error: { type: "os", code: "ENOTDIR" } });
    expect(await walk({ key: `${folder}/missing` })).toMatchObject({ error: { type: "os", code: "ENOENT" } });
    expect(await walk({ key: join(base, "outside") })).toMatchObject({ error: { type: "sandbox" } });
    for (const args of [
      { skip: "node_modules" }, { skip: [1] }, { skip_top: "_whiteboard" }, { skip_hidden: 1 }, { since: "yesterday" },
    ]) {
      expect(await walk(args)).toEqual({ error: { type: "value", message: BAD_WALK } });
    }
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
