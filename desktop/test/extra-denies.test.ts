import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Failure } from "../src/files/answers.js";
import type { HostStart } from "../src/hosts/messages.js";
import { MAX_EXTRA_DENIES, extraDenies, protectedKeys } from "../src/hosts/restarts.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

type Answer = { ok?: any; error?: { type: string; message: string } };

let base: string;
let folder: string;
let harnesses: Harness[];
let next = 0;
// chmod 000 does not stop root from reading.
const asRoot = process.getuid?.() === 0;

const chain = (letter: string, count: number) => Array.from({ length: count }, (_, i) => `${letter}${i + 1}`).join("/");

// A file at *rel* in the folder, its folders made.
function file(rel: string): string {
  const path = join(folder, rel);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, "");
  return path;
}

function refusal(run: () => unknown): unknown {
  try {
    run();
  } catch (error) {
    return error instanceof Failure ? error.refusal : error;
  }
  return null;
}

async function host(): Promise<Harness> {
  const harness = new Harness();
  harnesses.push(harness);
  const start: HostStart = {
    type: "start",
    folder,
    expect: bound(folder),
    domains: [],
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harness.send(start);
  await harness.until((messages) => messages.find((message) => message.type === "ready"));
  return harness;
}

const ask = (harness: Harness, kind: string, args: Record<string, unknown>) => harness.op(`ed-${next++}`, kind, args) as Promise<Answer>;
const begin = (harness: Harness, command: string) => ask(harness, "start", { command, workdir: null, task_id: "t", pty: false });
const run = (harness: Harness, command: string) => ask(harness, "run", { command, workdir: null, timeout: 10 });
const tryWrite = (path: string) => `if { printf x >> ${path}; } 2>/dev/null; then echo written; else echo denied; fi`;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "extra-denies-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  mkdirSync(join(base, "home"));
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("extra denies", () => {
  it("are the outermost protected path of each key srt's own rules miss, at any depth, never a .git folder", async () => {
    const deep = chain("d", 10);
    // srt's own, from the folder's top alone: its names there in its spelling, and the
    // top repository's config and hooks.
    file(".git/config");
    file(".git/hooks/pre-commit");
    file(".vscode/settings.json");
    file(".bashrc");
    // Not srt's: its nested scan, which it drops when rg fails; git's other config
    // and state, a .git file, a nested hooks folder, its names inside a .git folder,
    // and its names in another case.
    const zshrc = file("sub/.zshrc");
    const bashrc = file("sub/.bashrc");
    const subConfig = file("sub/.git/config");
    const shallow = file(`${chain("s", 8)}/.git/config`);
    const eleven = file(`${deep}/.bashrc`);
    const worktree = file(".git/config.worktree");
    file(".git/rebase-merge/done");
    file(".git/worktrees/w/commondir");
    file(".git/modules/m/config");
    const gitfile = file("wt/.git");
    file("sub/.git/hooks/pre-commit");
    const inGit = file("sub/.git/.gitconfig");
    const upper = file("up/.GIT/config");
    file(".VSCODE/x");
    file(`${deep}/.git/config`);
    file(`${deep}/.vscode/a`);
    expect(extraDenies(folder, await protectedKeys(folder))).toEqual([
      worktree,
      join(folder, ".git", "modules", "m", "config"),
      join(folder, ".git", "rebase-merge"),
      join(folder, ".git", "worktrees"),
      join(folder, ".VSCODE"),
      eleven,
      join(folder, deep, ".git", "config"),
      join(folder, deep, ".vscode"),
      shallow,
      bashrc,
      inGit,
      subConfig,
      join(folder, "sub", ".git", "hooks"),
      zshrc,
      upper,
      gitfile,
    ].sort());
  });

  it("refuse a protected path whose name srt would read as a glob, naming it", () => {
    const key = join(folder, "app", "[id]", ".git");
    expect(refusal(() => extraDenies(folder, [key]))).toEqual({
      type: "sandbox",
      message: "Blocked: this computer cannot protect app/[id]/.git in the sandbox, because its name holds *, ?, [ or ], so background processes cannot start here.",
    });
  });

  it(`refuse a folder with more than ${MAX_EXTRA_DENIES} of them, naming how many`, () => {
    const keys = Array.from({ length: MAX_EXTRA_DENIES + 1 }, (_, i) => join(folder, chain("d", 10), `p${i}`, ".vscode", "a"));
    expect(extraDenies(folder, keys.slice(0, MAX_EXTRA_DENIES))).toHaveLength(MAX_EXTRA_DENIES);
    expect(refusal(() => extraDenies(folder, keys))).toEqual({
      type: "sandbox",
      message: `Blocked: this folder has ${MAX_EXTRA_DENIES + 1} protected paths the sandbox does not cover by itself, and the computer can protect at most ${MAX_EXTRA_DENIES}, so background processes cannot start here.`,
    });
  });

  it("come from a walk that fails closed when it takes too long", async () => {
    for (let i = 0; i < 300; i += 1) mkdirSync(join(folder, `d${i}`, "e"), { recursive: true });
    await expect(protectedKeys(folder, 0)).rejects.toMatchObject({ refusal: { type: "sandbox", message: expect.stringContaining("within 0 seconds") } });
  });

  it.skipIf(asRoot)("come from a walk that fails closed when it cannot read a folder", async () => {
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    try {
      await expect(protectedKeys(folder)).rejects.toMatchObject({
        refusal: {
          type: "sandbox",
          message: "Blocked: the computer cannot read locked in this folder, so it cannot tell which paths to protect there, and background processes cannot start here. Make it readable to start them.",
        },
      });
    } finally {
      chmodSync(locked, 0o755);
    }
  });
});

describe("a session runner's wrap", { timeout: 30_000 }, () => {
  it("protects git's other config and a nested repository deeper than srt's scan reaches", async () => {
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", folder]);
    writeFileSync(join(folder, ".git", "config.worktree"), "");
    const deep = chain("n", 12);
    mkdirSync(join(folder, deep), { recursive: true });
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", join(folder, deep)]);
    const harness = await host();
    // Without a runner, srt's own wrap leaves them writable.
    expect((await run(harness, `${tryWrite(".git/config.worktree")}; ${tryWrite(`${deep}/.git/config`)}`)).ok?.output).toBe("written\nwritten\n");
    expect((await begin(harness, "sleep 631")).ok?.session_id).toBeDefined();
    expect((await run(harness, tryWrite(".git/config.worktree"))).ok?.output).toBe("denied\n");
    expect((await run(harness, tryWrite(`${deep}/.git/config`))).ok?.output).toBe("denied\n");
    expect((await run(harness, tryWrite(`${deep}/.git/hooks/pre-commit`))).ok?.output).toBe("denied\n");
  });

  it.skipIf(asRoot)("protects a nested repository's config when srt's own scan fails on a folder it cannot read", async () => {
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", folder]);
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", join(folder, "sub")]);
    const config = join(folder, "sub", ".git", "config");
    const before = readFileSync(config, "utf8");
    // In the object store, which the walk skips and srt's rg does not.
    const locked = join(folder, ".git", "objects", "zz");
    mkdirSync(locked);
    chmodSync(locked, 0);
    try {
      const harness = await host();
      expect((await begin(harness, "sleep 630")).ok?.session_id).toBeDefined();
      expect((await run(harness, tryWrite("sub/.git/config"))).ok?.output).toBe("denied\n");
      expect(readFileSync(config, "utf8")).toBe(before);
    } finally {
      chmodSync(locked, 0o755);
    }
  });

  it("blocks commands and refuses to start in a folder holding a name that is not valid UTF-8", async () => {
    // The walk reads the name back with U+FFFD, and no path it can build reaches it.
    const named = Buffer.concat([Buffer.from(`${folder}/`), Buffer.from([0xff])]);
    mkdirSync(named);
    try {
      const harness = await host();
      const blocked = { error: { type: "sandbox", message: expect.stringContaining("cannot read \uFFFD in this folder") } };
      expect(await run(harness, "echo hi")).toEqual(blocked);
      expect(await begin(harness, "true")).toEqual(blocked);
    } finally {
      rmdirSync(named);
    }
  });

  it("refuses to start with a protected path srt would read as a glob, and commands still run", async () => {
    file("app/[id]/.git");
    const harness = await host();
    expect(await begin(harness, "true")).toEqual({
      error: { type: "sandbox", message: expect.stringContaining("cannot protect app/[id]/.git in the sandbox") },
    });
    expect((await run(harness, "echo hi")).ok?.output).toBe("hi\n");
  });

  it(`refuses to start past ${MAX_EXTRA_DENIES} protected paths srt misses, and commands still run`, async () => {
    for (let i = 0; i <= MAX_EXTRA_DENIES; i += 1) file(`${chain("d", 10)}/p${i}/.vscode/a`);
    const harness = await host();
    expect(await begin(harness, "sleep 632")).toEqual({
      error: { type: "sandbox", message: expect.stringContaining(`this folder has ${MAX_EXTRA_DENIES + 1} protected paths`) },
    });
    expect((await run(harness, "echo hi")).ok?.output).toBe("hi\n");
  });
});
