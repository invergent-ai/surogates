// The file kinds on Windows, on the runner's own NTFS, through the walk (src/files/fd-paths-win32.ts): what they do in
// a folder, what they refuse of a junction or a link in it, of Windows's names and of Explorer's, how a search is given
// its folder and held there, and the helper's look at its folder before each request. It needs dist/ (tsc), the app's
// rg at bin/rg.exe (scripts/rg.sh) and dist/native/ntat.node, and runs on Windows alone.

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { built } from "./built.js";

const WINDOWS = process.platform === "win32";
const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const BIN = join(DESKTOP, "bin");

async function fresh() {
  vi.resetModules();
  const [walk, { perform }, { FOLDER_UNAVAILABLE }] = await Promise.all([
    import("../src/files/fd-paths-win32.js"), import("../src/files/operations.js"), import("../src/files/answers.js"),
  ]);
  return { ...walk, fdPaths: walk.win32FdPaths, perform, FOLDER_UNAVAILABLE };
}

const b64 = (text: string) => Buffer.from(text).toString("base64");
const signal = () => new AbortController().signal;

let base: string;
let host: string;
let outside: string;

beforeEach(() => {
  if (!WINDOWS) return;
  base = realpathSync.native(mkdtempSync(join(tmpdir(), "file-host-")));
  host = join(base, "folder");
  outside = join(base, "outside");
  mkdirSync(host);
  mkdirSync(outside);
  writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
});

afterEach(() => {
  if (WINDOWS) rmSync(base, { recursive: true, force: true });
});

describe.runIf(WINDOWS)("the file kinds on Windows", { timeout: 60_000 }, () => {
  it("work in the folder by its path in the guest", async () => {
    const { fdPaths, guestPathOf, perform } = await fresh();
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const context = { folder: root, home: root, env: { PATH: BIN } };
    const answer = (kind: string, args: Record<string, unknown>) => perform(kind, args, context, signal());
    expect(await answer("resolve", { path: "src/a.txt" })).toEqual({ ok: `${root}/src/a.txt` });
    expect(await answer("write", { key: `${root}/src/a.txt`, data: b64("one\ntwo\n") })).toEqual({ ok: null });
    expect(readFileSync(join(host, "src", "a.txt"), "utf8")).toBe("one\ntwo\n");
    expect(await answer("read", { key: `${root}/src/a.txt`, max_bytes: null })).toEqual({ ok: b64("one\ntwo\n") });
    expect(await answer("read_lines", { key: `${root}/src/a.txt`, encoding: "utf-8", offset: 2, limit: 1, max_bytes: 100 })).toEqual({ ok: { data: b64("two\n"), total_lines: 2 } });
    const stat = await answer("stat", { key: `${root}/src/a.txt` });
    expect(stat).toMatchObject({ ok: { is_dir: false, size: 8 } });
    // A write that expects the revision it read.
    const revision = (stat as { ok: { revision: string } }).ok.revision;
    expect(await answer("write", { key: `${root}/src/a.txt`, data: b64("three\n"), expected_revision: revision })).toEqual({ ok: null });
    expect(await answer("write", { key: `${root}/src/a.txt`, data: b64("four\n"), expected_revision: revision })).toMatchObject({ error: { type: "conflict" } });
    expect(await answer("list_dir", { key: root })).toEqual({ ok: ["src"] });
    expect(await answer("walk", { key: root, skip: [], skip_top: [], skip_hidden: false, since: null })).toMatchObject({ ok: { files: [["src/a.txt", 6]], truncated: false } });
    expect(await answer("ripgrep", { key: root, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${root}/src/a.txt\n` });
    expect(await answer("ripgrep", { key: root, mode: "count", pattern: "three", glob: null, context: 0 })).toEqual({ ok: `${root}/src/a.txt:1\n` });
    const events = await answer("ripgrep", { key: `${root}/src`, mode: "json", pattern: "three", glob: null, context: 0 });
    expect((events as { ok: string }).ok).toContain(JSON.stringify(`${root}/src/a.txt`));
    expect(await answer("delete", { key: `${root}/src/a.txt` })).toEqual({ ok: null });
    expect(readdirSync(join(host, "src"))).toEqual([]);
  });

  it("reach nothing through a junction or a link in the folder, and search past it", async () => {
    const { fdPaths, guestPathOf, perform } = await fresh();
    symlinkSync(outside, join(host, "junction"), "junction");
    symlinkSync(outside, join(host, "dirlink"), "dir");
    writeFileSync(join(host, "inside.txt"), "OUTSIDE is named here\n");
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const answer = (kind: string, args: Record<string, unknown>) => perform(kind, args, { folder: root, home: root, env: { PATH: BIN } }, signal());
    for (const link of ["junction", "dirlink"]) {
      for (const [kind, args] of [
        ["read", { key: `${root}/${link}/only-outside`, max_bytes: null }], ["write", { key: `${root}/${link}/new`, data: b64("x") }],
        ["list_dir", { key: `${root}/${link}` }], ["delete", { key: `${root}/${link}/only-outside` }], ["stat", { key: `${root}/${link}/only-outside` }],
        ["ripgrep", { key: `${root}/${link}`, mode: "count", pattern: "OUTSIDE", glob: null, context: 0 }],
      ] as const) {
        const outcome = await answer(kind, args);
        expect("ok" in outcome && outcome.ok !== null, `${kind} through ${link}`).toBe(false);
      }
    }
    expect(await answer("walk", { key: root, skip: [], skip_top: [], skip_hidden: false, since: null })).toMatchObject({ ok: { files: [["inside.txt", 22]] } });
    // rg is given the folder and follows no link in it.
    expect(await answer("ripgrep", { key: root, mode: "count", pattern: "OUTSIDE", glob: null, context: 0 })).toEqual({ ok: `${root}/inside.txt:1\n` });
    expect(readdirSync(outside)).toEqual(["only-outside"]);
    expect(readFileSync(join(outside, "only-outside"), "utf8")).toBe("OUTSIDE\n");
  });

  it("refuse each name Windows aliases, for reads and writes, and a write of a name Explorer acts on", async () => {
    const { fdPaths, guestPathOf, perform } = await fresh();
    mkdirSync(join(host, ".git", "hooks"), { recursive: true });
    writeFileSync(join(host, "pre-commit"), "kept\n");
    writeFileSync(join(host, "desktop.ini"), "[.ShellClassInfo]\n");
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const answer = (kind: string, args: Record<string, unknown>) => perform(kind, args, { folder: root, home: root, env: { PATH: BIN } }, signal());
    for (const [name, refused] of [
      [".git./hooks/pre-commit", ".git."], [".git /hooks/pre-commit", ".git "], ["GIT~1/hooks/pre-commit", "GIT~1"], [".git::$INDEX_ALLOCATION/hooks/x", ".git::$INDEX_ALLOCATION"],
      ["pre-commit::$DATA", "pre-commit::$DATA"], ["NUL", "NUL"], ["con.txt", "con.txt"], ["a\\b", "a\\b"],
    ] as const) {
      const message = `This name cannot be used on Windows: ${refused}`;
      expect(await answer("resolve", { path: name }), name).toEqual({ error: { type: "sandbox", message } });
      expect(await answer("write", { key: `${root}/${name}`, data: b64("x") }), name).toMatchObject({ error: { message } });
      expect(await answer("read", { key: `${root}/${name}`, max_bytes: null }), name).toMatchObject({ error: { message } });
    }
    expect(readdirSync(join(host, ".git", "hooks"))).toEqual([]);
    expect(readFileSync(join(host, "pre-commit"), "utf8")).toBe("kept\n");
    for (const name of ["desktop.ini", "Autorun.INF", "x.lnk", "sub/y.url", "a.library-ms"]) {
      expect(await answer("write", { key: `${root}/${name}`, data: b64("x") }), name).toEqual({
        error: { type: "sandbox", message: `Write denied: '${root}/${name}' is a name Windows's Explorer acts on when it shows this folder.` },
      });
      expect(await answer("check_write", { path: name }), name).toEqual({ ok: `Write denied: '${name}' is a name Windows's Explorer acts on when it shows this folder.` });
    }
    expect(await answer("delete", { key: `${root}/desktop.ini` })).toMatchObject({ error: { type: "sandbox" } });
    expect(await answer("read", { key: `${root}/desktop.ini`, max_bytes: null })).toEqual({ ok: b64("[.ShellClassInfo]\n") });
  });

  it("holds each folder on a search's way while it runs, so none of them can be renamed or removed, and lets go after", async () => {
    const { fdPaths, guestPathOf } = await fresh();
    mkdirSync(join(host, "a", "b"), { recursive: true });
    writeFileSync(join(host, "a", "b", "x"), "x");
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const held = fdPaths.outside(`${root}/a/b`);
    expect([held.path, held.empty]).toEqual([`${host.replaceAll("\\", "/")}/a/b`, false]);
    for (const [from, to] of [[join(host, "a"), join(host, "a2")], [join(host, "a", "b"), join(host, "b2")], [join(host, "a", "b", "x"), join(host, "x2")]]) {
      expect(() => renameSync(from!, to!), from).toThrow();
    }
    expect(() => rmSync(join(host, "a"), { recursive: true })).toThrow();
    held.release();
    renameSync(join(host, "a"), join(host, "a2"));
    // A folder with nothing in it holds nothing to find, and is searched for nothing.
    mkdirSync(join(host, "empty"));
    const empty = fdPaths.outside(`${root}/empty`);
    expect(empty.empty).toBe(true);
    empty.release();
  });
});

describe.runIf(WINDOWS)("the file helper on Windows", { timeout: 60_000 }, () => {
  let helper: ChildProcess | null = null;
  afterEach(() => {
    helper?.kill();
    helper = null;
  });

  async function started(env: Record<string, string>) {
    const child = spawn(process.execPath, [built(join(DESKTOP, "dist", "files", "helper.js"))], { env: { SystemRoot: process.env.SystemRoot ?? "", ...env }, stdio: ["pipe", "pipe", "pipe"] });
    helper = child;
    let stderr = "";
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()));
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    const next = async () => (await lines.next()).value as string | undefined;
    const exited = new Promise<number | null>((resolve) => child.once("exit", resolve));
    return { child, next, exited, stderr: () => stderr };
  }

  it("answers a request in a folder moved away and another made at its path as the folder unavailable", async () => {
    const { fdPaths, guestPathOf, FOLDER_UNAVAILABLE } = await fresh();
    writeFileSync(join(host, "a"), "a");
    const root = guestPathOf(host)!;
    fdPaths.bind([root]);
    const is = fdPaths.lstat(root);
    const run = await started({ SUROGATE_FOLDER: root, HOME: root, SUROGATE_FOLDER_IS: `${Number(is.dev)}:${Number(is.ino)}`, PATH: BIN });
    expect(await run.next()).toBe('{"ready":true}');
    run.child.stdin!.write(`${JSON.stringify({ id: "1", kind: "read", args: { key: `${root}/a`, max_bytes: null } })}\n`);
    expect(JSON.parse((await run.next())!)).toEqual({ id: "1", outcome: { ok: b64("a") } });
    renameSync(host, join(base, "moved"));
    mkdirSync(host);
    writeFileSync(join(host, "a"), "another");
    run.child.stdin!.write(`${JSON.stringify({ id: "2", kind: "read", args: { key: `${root}/a`, max_bytes: null } })}\n`);
    expect(JSON.parse((await run.next())!)).toEqual({ id: "2", outcome: FOLDER_UNAVAILABLE });
  });

  it("starts in no folder but the one its host checked, and lands nothing yet", async () => {
    const { guestPathOf } = await fresh();
    const root = guestPathOf(host)!;
    const wrong = await started({ SUROGATE_FOLDER: root, HOME: root, SUROGATE_FOLDER_IS: "1:2", PATH: BIN });
    expect(await wrong.exited).toBe(2);
    expect(wrong.stderr()).toMatch(/is not the folder its host checked/);
    const landing = await started({ SUROGATE_FOLDER: root, HOME: root, SUROGATE_COPY: root, SUROGATE_KEPT: root, PATH: BIN });
    expect(await landing.exited).toBe(2);
    expect(landing.stderr()).toMatch(/Landing a thread's work is not built for Windows yet/);
  });
});
