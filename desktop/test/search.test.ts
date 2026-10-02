import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OUTPUT_CAP_CHARS } from "../src/files/answers.js";
import { type Context, findOnPath, perform, RG_MISSING } from "../src/files/operations.js";

let base: string;
let folder: string;
let context: Context;

const run = (kind: string, args: Record<string, unknown>, signal = new AbortController().signal) =>
  perform(kind, args, context, signal);
const search = (args: Record<string, unknown>) => run("ripgrep", { glob: null, context: 0, ...args });
const lines = (outcome: unknown) => ((outcome as { ok: string }).ok).split("\n").filter(Boolean).sort();

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "search-")));
  folder = join(base, "folder");
  mkdirSync(join(folder, "sub"), { recursive: true });
  writeFileSync(join(folder, "a.txt"), "alpha\nbeta\n");
  writeFileSync(join(folder, "sub", "b.txt"), "beta gamma\n");
  writeFileSync(join(folder, "sub", "c.md"), "gamma\n");
  // rg's -g overrides its rule for hidden files, but a hidden folder whose name
  // does not match stays skipped (the cloud's rg does the same).
  mkdirSync(join(folder, ".hidden"));
  writeFileSync(join(folder, ".hidden", "h.txt"), "beta\n");
  context = { folder, home: "/home/tester", env: { PATH: "/usr/bin:/bin", HOME: "/home/tester" } };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("ripgrep, with the cloud's arguments", () => {
  it("lists files matching a glob, skipping hidden folders", async () => {
    expect(lines(await search({ key: folder, mode: "files", pattern: "*.txt" }))).toEqual([
      `${folder}/a.txt`, `${folder}/sub/b.txt`,
    ]);
  });

  it("counts matches per file", async () => {
    expect(lines(await search({ key: folder, mode: "count", pattern: "beta", glob: "*.txt" }))).toEqual([
      `${folder}/a.txt:1`, `${folder}/sub/b.txt:1`,
    ]);
  });

  it("answers matches as rg's JSON events, with context", async () => {
    const events = lines(await search({ key: `${folder}/sub`, mode: "json", pattern: "gamma", context: 1 }))
      .map((line) => JSON.parse(line) as { type: string; data: { path?: { text: string } } });
    expect(events.filter((event) => event.type === "match").map((event) => event.data.path?.text).sort()).toEqual([
      `${folder}/sub/b.txt`, `${folder}/sub/c.md`,
    ]);
  });

  it("answers a pattern rg refuses with rg's exit code and the head of its message", async () => {
    const answer = await search({ key: folder, mode: "count", pattern: "(" });
    expect(answer).toMatchObject({ error: { type: "ripgrep" } });
    expect((answer as { error: { message: string } }).error.message).toMatch(/^rg exited 2: /);
  });

  it("answers output over the cap with a request to narrow it, never a cut", async () => {
    writeFileSync(join(folder, "many.txt"), "beta\n".repeat(OUTPUT_CAP_CHARS));
    expect(await search({ key: folder, mode: "json", pattern: "beta" })).toEqual({
      error: {
        type: "ripgrep",
        message: `search output over ${OUTPUT_CAP_CHARS} characters; narrow the pattern, path or glob`,
      },
    });
  });

  it("measures the output as Python's json.dumps does, not in bytes", async () => {
    writeFileSync(join(folder, "wide.txt"), `${"é".repeat(50_000)}\n`);
    expect(await search({ key: folder, mode: "json", pattern: "é+" })).toEqual({
      error: {
        type: "ripgrep",
        message: `search output over ${OUTPUT_CAP_CHARS} characters; narrow the pattern, path or glob`,
      },
    });
  });

  it("says when rg is not on this computer", async () => {
    context.env.PATH = join(base, "empty");
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({
      error: { type: "ripgrep", message: RG_MISSING },
    });
  });

  it("stops a search when the operation is cancelled", async () => {
    mkdirSync(join(base, "bin"));
    writeFileSync(join(base, "bin", "rg"), "#!/bin/sh\nexec sleep 30\n", { mode: 0o755 });
    context.env.PATH = `${join(base, "bin")}:/usr/bin:/bin`;
    const controller = new AbortController();
    const started = Date.now();
    const answer = run("ripgrep", { key: folder, mode: "files", pattern: "*", glob: null, context: 0 }, controller.signal);
    setTimeout(() => controller.abort(), 100);
    expect(await answer).toMatchObject({ error: { type: "cancelled" } });
    expect(Date.now() - started).toBeLessThan(2000);
  });

  it("refuses bad arguments", async () => {
    expect(await search({ key: folder, mode: "regex", pattern: "a" })).toMatchObject({ error: { type: "value" } });
    expect(await search({ key: folder, mode: "count", pattern: "a\0" })).toEqual({
      error: { type: "value", message: "embedded null byte" },
    });
    expect(await search({ key: `${base}`, mode: "files", pattern: "*" })).toMatchObject({ error: { type: "sandbox" } });
  });
});

describe("which, as shutil.which", () => {
  it("finds a command on the PATH, or by its path", async () => {
    expect(await run("which", { name: "sh" })).toEqual({ ok: true });
    expect(await run("which", { name: "/bin/sh" })).toEqual({ ok: true });
    expect(await run("which", { name: "no-such-command-zz" })).toEqual({ ok: false });
  });

  it("finds nothing for an empty name, a NUL, an empty PATH, a folder or a file that does not run", async () => {
    expect(await run("which", { name: "" })).toEqual({ ok: false });
    expect(await run("which", { name: "s\0h" })).toEqual({ ok: false });
    mkdirSync(join(base, "bin", "tool"), { recursive: true });
    writeFileSync(join(base, "bin", "plain"), "x", { mode: 0o644 });
    context.env.PATH = join(base, "bin");
    expect(await run("which", { name: "tool" })).toEqual({ ok: false });
    expect(await run("which", { name: "plain" })).toEqual({ ok: false });
    context.env.PATH = "";
    expect(await run("which", { name: "sh" })).toEqual({ ok: false });
  });

  it("returns the path it found", () => {
    expect(findOnPath("sh", "/usr/bin:/bin", folder)).toMatch(/\/sh$/);
    expect(findOnPath("no-such-command-zz", "/usr/bin:/bin", folder)).toBeNull();
  });
});
