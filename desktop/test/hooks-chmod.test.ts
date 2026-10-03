// What the hook guard does when chmod fails, and when two looks overlap. The
// calls are real unless a test switches chmod to fail or holds a readdir.

import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import type { Mode, PathLike } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HookGuard, neutralize, scanHooks } from "../src/hosts/hooks.js";

const calls = vi.hoisted(() => ({
  failing: (_path: string): boolean => false,
  // The readdir of *path* after *skip* others of it waits for *release*, and
  // says when it started waiting.
  held: null as { path: string; skip: number; reached: () => void; release: Promise<void> } | null,
}));

vi.mock("node:fs/promises", async (original) => {
  const fs = await original<typeof import("node:fs/promises")>();
  return {
    ...fs,
    chmod: async (path: PathLike, mode: Mode) => {
      if (calls.failing(String(path))) {
        throw Object.assign(new Error("EPERM: operation not permitted, chmod"), { code: "EPERM", syscall: "chmod" });
      }
      return fs.chmod(path, mode);
    },
    readdir: async (...args: Parameters<typeof fs.readdir>) => {
      const held = calls.held;
      if (held && String(args[0]) === held.path && held.skip-- === 0) {
        calls.held = null;
        held.reached();
        await held.release;
      }
      return fs.readdir(...args);
    },
  };
});

let folder: string;

function hook(rel: string): string {
  const path = join(folder, rel);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, "#!/bin/sh\n");
  chmodSync(path, 0o755);
  return path;
}
const executable = (path: string) => (statSync(path).mode & 0o111) !== 0;
const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });
// chmod 000 does not stop root from reading.
const asRoot = process.getuid?.() === 0;

beforeEach(() => {
  folder = realpathSync(mkdtempSync(join(tmpdir(), "hooks-chmod-")));
  calls.failing = () => false;
  calls.held = null;
});

afterEach(() => {
  calls.failing = () => false;
  rmSync(folder, { recursive: true, force: true });
});

describe("a hook chmod cannot change", () => {
  it("has its hooks folder closed instead", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const added = hook(".git/hooks/pre-commit");
    calls.failing = (path) => path === added;
    try {
      expect(await neutralize(folder, await scanHooks(folder), baseline)).toEqual({ changed: [added], stuck: [] });
      expect(statSync(dirname(added)).mode & 0o111).toBe(0);
    } finally {
      chmodSync(dirname(added), 0o755);
    }
  });

  it("is stuck when its hooks folder cannot be closed either", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const added = hook(".git/hooks/pre-commit");
    calls.failing = () => true;
    expect(await neutralize(folder, await scanHooks(folder), baseline)).toEqual({ changed: [], stuck: [added] });
    expect(executable(added)).toBe(true);
  });

  it("stops commands, naming it, until it can be made non-executable", async () => {
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toBeNull();
    const added = hook(".git/hooks/pre-commit");
    calls.failing = () => true;
    expect(await guard.after(ran("done"))).toEqual(ran("done"));
    expect(await guard.refusal()).toEqual({
      error: {
        type: "sandbox",
        message: "Blocked: the computer could not stop these git hooks from running outside the sandbox: .git/hooks/pre-commit. Remove them or make them non-executable to run commands here.",
      },
    });
    calls.failing = () => false;
    expect(await guard.refusal()).toBeNull();
    expect(executable(added)).toBe(false);
  });

  it.skipIf(asRoot)("stops commands giving both reasons while a folder cannot be read too", async () => {
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toBeNull();
    hook(".git/hooks/pre-commit");
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    calls.failing = () => true;
    try {
      await guard.after(ran(""));
      expect(await guard.refusal()).toEqual({
        error: {
          type: "sandbox",
          message: "Blocked: the computer could not stop these git hooks from running outside the sandbox: .git/hooks/pre-commit. Remove them or make them non-executable to run commands here. Blocked: the computer cannot read locked in this folder, so it cannot check there for git hooks, which would run outside the sandbox. Make it readable to run commands here.",
        },
      });
    } finally {
      chmodSync(locked, 0o755);
    }
  });
});

describe("overlapping looks", () => {
  it.skipIf(asRoot)("let only the newest say whether commands may run", async () => {
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toBeNull();
    const slow = join(folder, "slow");
    mkdirSync(slow);
    let release = () => {};
    const reached = new Promise<void>((resolve) => {
      calls.held = { path: slow, skip: 0, reached: resolve, release: new Promise((done) => { release = done; }) };
    });
    // An older look listed the folder before it held anything unreadable...
    const older = guard.after(ran(""));
    await reached;
    // ...and a newer one cannot read a folder in it.
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    try {
      expect(await guard.settle()).toBe(false);
      release();
      await older;
      expect(await guard.refusal()).toEqual({
        error: { type: "sandbox", message: expect.stringContaining("locked") },
      });
    } finally {
      release();
      chmodSync(locked, 0o755);
    }
  });

  it.skipIf(asRoot)("let settle judge the stop by its own look, though a newer one started", async () => {
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toBeNull();
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    let release = () => {};
    const reached = new Promise<void>((resolve) => {
      // Settle's look lists the folder first; the newer look's listing waits.
      calls.held = { path: folder, skip: 1, reached: resolve, release: new Promise((done) => { release = done; }) };
    });
    try {
      const settled = guard.settle();
      const newer = guard.after(ran(""));
      await reached;
      expect(await settled).toBe(false);
      release();
      await newer;
    } finally {
      release();
      chmodSync(locked, 0o755);
    }
  });
});
