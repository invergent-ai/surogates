import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { NUL_REFUSED, OUTPUT_CAP_CHARS } from "../src/files/answers.js";
import { type Context, findOnPath, perform, RG_MISSING } from "../src/files/operations.js";

let base: string;
let folder: string;
let context: Context;

const run = (kind: string, args: Record<string, unknown>, signal = new AbortController().signal) =>
  perform(kind, args, context, signal);
const search = (args: Record<string, unknown>) => run("ripgrep", { glob: null, context: 0, ...args });
const lines = (outcome: unknown) => ((outcome as { ok: string }).ok).split("\n").filter(Boolean).sort();
const NARROW = `search output over ${OUTPUT_CAP_CHARS} characters; narrow the pattern, path or glob`;

// An rg that is this script, first on the PATH. It writes its pid to base/pid before the rest runs.
function fakeRg(script: string): string {
  const pidfile = join(base, "pid");
  mkdirSync(join(base, "bin"), { recursive: true });
  writeFileSync(join(base, "bin", "rg"), `#!/bin/sh\necho $$ > "${pidfile}"\n${script}\n`, { mode: 0o755 });
  context.env.PATH = `${join(base, "bin")}:/usr/bin:/bin`;
  return pidfile;
}

async function until(condition: () => boolean, ms = 2000): Promise<boolean> {
  for (const end = Date.now() + ms; Date.now() < end; await new Promise((done) => setTimeout(done, 20))) {
    if (condition()) return true;
  }
  return condition();
}

const pidOf = (pidfile: string) => Number(readFileSync(pidfile, "utf8"));
const written = (pidfile: string) => () => {
  try {
    return readFileSync(pidfile, "utf8").endsWith("\n");
  } catch {
    return false;
  }
};
const gone = (pid: number) => () => {
  try {
    process.kill(pid, 0);
    return false;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "ESRCH";
  }
};

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "search-")));
  folder = join(base, "folder");
  mkdirSync(join(folder, "sub"), { recursive: true });
  writeFileSync(join(folder, "a.txt"), "alpha\nbeta\n");
  writeFileSync(join(folder, "sub", "b.txt"), "beta gamma\n");
  writeFileSync(join(folder, "sub", "c.md"), "gamma\nbeta\n");
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

  it("counts in every file without a glob, and only the glob's files with one", async () => {
    expect(lines(await search({ key: folder, mode: "count", pattern: "beta" }))).toEqual([
      `${folder}/a.txt:1`, `${folder}/sub/b.txt:1`, `${folder}/sub/c.md:1`,
    ]);
  });

  it("answers rg's context events only when asked for context", async () => {
    mkdirSync(join(folder, "ctx"));
    writeFileSync(join(folder, "ctx", "lines.txt"), "x1\ngamma\nx2\n");
    const types = async (context: number) =>
      new Set(lines(await search({ key: `${folder}/ctx`, mode: "json", pattern: "gamma", context }))
        .map((line) => (JSON.parse(line) as { type: string }).type));
    expect(await types(1)).toContain("context");
    expect(await types(0)).not.toContain("context");
  });

  it("answers nothing, not an error, when nothing matches", async () => {
    expect(await search({ key: folder, mode: "count", pattern: "no-such-text-zz" })).toEqual({ ok: "" });
    expect(await search({ key: folder, mode: "files", pattern: "*.nothing" })).toEqual({ ok: "" });
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

  it("stops a search when the operation is cancelled, and rg with it", async () => {
    const pidfile = fakeRg("exec sleep 30");
    const controller = new AbortController();
    const started = Date.now();
    const answer = run("ripgrep", { key: folder, mode: "files", pattern: "*", glob: null, context: 0 }, controller.signal);
    expect(await until(written(pidfile))).toBe(true);
    controller.abort();
    expect(await answer).toMatchObject({ error: { type: "cancelled" } });
    expect(Date.now() - started).toBeLessThan(2000);
    expect(await until(gone(pidOf(pidfile)))).toBe(true);
  });

  it("answers cancelled at once for a signal that is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const started = Date.now();
    expect(await run("ripgrep", { key: folder, mode: "files", pattern: "*", glob: null, context: 0 }, controller.signal))
      .toMatchObject({ error: { type: "cancelled" } });
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it("answers as soon as the output is over the cap, and stops rg", async () => {
    const pidfile = fakeRg(`head -c ${OUTPUT_CAP_CHARS + 40_000} /dev/zero | tr '\\0' x\nexec sleep 30`);
    const started = Date.now();
    expect(await search({ key: folder, mode: "json", pattern: "x" })).toEqual({ error: { type: "ripgrep", message: NARROW } });
    expect(Date.now() - started).toBeLessThan(2000);
    expect(await until(gone(pidOf(pidfile)))).toBe(true);
  });

  it("answers a spawn that fails for want of rg as rg missing, and any other as an os error", async () => {
    fakeRg("exit 0");
    writeFileSync(join(base, "bin", "rg"), "#!/nonexistent/interp\n", { mode: 0o755 });
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({
      error: { type: "ripgrep", message: RG_MISSING },
    });
    context.env.PATH = "/usr/bin:/bin";
    const answer = await search({ key: folder, mode: "count", pattern: "a".repeat(200 * 1024) });
    expect(answer).toMatchObject({ error: { type: "os", code: "E2BIG" } });
    expect((answer as { error: { message: string } }).error.message).toMatch(/^Argument list too long: /);
  });

  it("keeps the first 200 characters of what rg says, however it writes them", async () => {
    fakeRg("i=0; while [ $i -lt 60 ]; do printf x >&2; i=$((i+1)); sleep 0.01; done; exit 2");
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({
      error: { type: "ripgrep", message: `rg exited 2: ${"x".repeat(60)}` },
    });
    fakeRg("head -c 2000 /dev/zero | tr '\\0' x >&2; exit 2");
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({
      error: { type: "ripgrep", message: `rg exited 2: ${"x".repeat(200)}` },
    });
    fakeRg("printf '\\303\\251%.0s' $(seq 300) >&2; exit 2");
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({
      error: { type: "ripgrep", message: `rg exited 2: ${"é".repeat(200)}` },
    });
  });

  it("keeps a byte order mark at the start of what rg prints", async () => {
    fakeRg("printf '\\357\\273\\277hello\\n'");
    expect(await search({ key: folder, mode: "files", pattern: "*" })).toEqual({ ok: "\uFEFFhello\n" });
  });

  it("refuses bad arguments", async () => {
    expect(await search({ key: folder, mode: "regex", pattern: "a" })).toMatchObject({ error: { type: "value" } });
    expect(await search({ key: folder, mode: "count", pattern: "a\0" })).toEqual({
      error: { type: "value", message: NUL_REFUSED },
    });
    expect(await search({ key: folder, mode: "count", pattern: "a", glob: "*\0" })).toEqual({
      error: { type: "value", message: NUL_REFUSED },
    });
    expect(await search({ key: `${base}`, mode: "files", pattern: "*" })).toMatchObject({ error: { type: "sandbox" } });
  });
});

describe("findOnPath, as shutil.which", () => {
  const found = (name: string, path: string | undefined, cwd = folder) => findOnPath(name, path, cwd) !== null;

  it("finds a command on the PATH, or by its path", () => {
    expect(found("sh", "/usr/bin:/bin")).toBe(true);
    expect(found("/bin/sh", "/usr/bin:/bin")).toBe(true);
    expect(found("no-such-command-zz", "/usr/bin:/bin")).toBe(false);
  });

  it("finds nothing for an empty name, a NUL, an empty PATH, a folder or a file that does not run", () => {
    expect(found("", "/usr/bin:/bin")).toBe(false);
    expect(found("s\0h", "/usr/bin:/bin")).toBe(false);
    mkdirSync(join(base, "bin", "tool"), { recursive: true });
    writeFileSync(join(base, "bin", "plain"), "x", { mode: 0o644 });
    expect(found("tool", join(base, "bin"))).toBe(false);
    expect(found("plain", join(base, "bin"))).toBe(false);
    expect(found("sh", "")).toBe(false);
  });

  it("finds a command with no PATH set, in /bin:/usr/bin, and in the folder through an empty entry", () => {
    expect(found("sh", undefined)).toBe(true);
    writeFileSync(join(folder, "tool"), "#!/bin/sh\n", { mode: 0o755 });
    expect(found("tool", ":/nonexistent")).toBe(true);
  });

  it("is not a kind of the file helper's: the guest answers which", async () => {
    expect(await run("which", { name: "sh" })).toEqual({ error: { type: "unsupported", message: "This computer cannot do 'which' yet" } });
  });

  it("reads a relative PATH entry from the folder, as it does an empty one", () => {
    writeFileSync(join(folder, "tool"), "#!/bin/sh\n", { mode: 0o755 });
    expect(findOnPath("tool", ".", folder)).toBe(join(folder, "tool"));
    expect(findOnPath("tool", ":", folder)).toBe(join(folder, "tool"));
    expect(findOnPath("tool", "sub/..:/nonexistent", folder)).toBe(join(folder, "tool"));
  });

  it("returns the path it found", () => {
    expect(findOnPath("sh", "/usr/bin:/bin", folder)).toMatch(/\/sh$/);
    expect(findOnPath("no-such-command-zz", "/usr/bin:/bin", folder)).toBeNull();
  });
});
