import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { HOOKS_NOTICE, HookGuard, isGitHook, neutralize, scanHooks } from "../src/hosts/hooks.js";

let folder: string;
let other: string;

function hook(rel: string, mode = 0o755, root = folder): string {
  const path = join(root, rel);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, "#!/bin/sh\n");
  chmodSync(path, mode);
  return path;
}
const executable = (path: string) => (statSync(path).mode & 0o111) !== 0;
const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });
// chmod 000 does not stop root from reading.
const asRoot = process.getuid?.() === 0;

beforeEach(() => {
  folder = realpathSync(mkdtempSync(join(tmpdir(), "hooks-")));
  // Another folder a command can write, as the session's temp folder is.
  other = realpathSync(mkdtempSync(join(tmpdir(), "hooks-tmp-")));
});

afterEach(() => {
  rmSync(folder, { recursive: true, force: true });
  rmSync(other, { recursive: true, force: true });
});

describe("a git hook", () => {
  it("is a file under a hooks folder in a .git folder, at any depth and in any case", () => {
    expect(isGitHook(folder, `${folder}/.git/hooks/pre-commit`)).toBe(true);
    expect(isGitHook(folder, `${folder}/a/b/c/d/e/f/.git/hooks/pre-commit`)).toBe(true);
    expect(isGitHook(folder, `${folder}/X/.GIT/Hooks/post-checkout`)).toBe(true);
    expect(isGitHook(folder, `${folder}/.git/modules/sub/hooks/pre-push`)).toBe(true);
    expect(isGitHook(folder, `${folder}/.git/hooks`)).toBe(false);
    expect(isGitHook(folder, `${folder}/hooks/pre-commit`)).toBe(false);
    expect(isGitHook(folder, `${folder}/.git/config`)).toBe(false);
    expect(isGitHook(folder, "/elsewhere/.git/hooks/pre-commit")).toBe(false);
  });
});

describe("the walk", () => {
  it("finds hooks at any depth, and skips node_modules and a git folder's object store", async () => {
    const deep = hook("a/b/c/d/e/f/.git/hooks/pre-commit");
    hook("node_modules/pkg/.git/hooks/pre-commit");
    hook("a/.git/objects/hooks/x");
    writeFileSync(join(folder, "a", ".git", "HEAD"), "ref: refs/heads/main\n");
    const scan = await scanHooks(folder);
    expect([...scan.hooks.keys()]).toEqual([deep]);
    expect(scan.unreadable).toEqual([]);
  });

  it("walks a submodule's git folder even when the submodule is named objects", async () => {
    mkdirSync(join(folder, ".git", "modules", "objects"), { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(folder, ".git", "modules", "objects", "HEAD"), "ref: refs/heads/main\n");
    const inner = hook(".git/modules/objects/hooks/pre-commit");
    expect([...(await scanHooks(folder)).hooks.keys()]).toEqual([inner]);
  });

  it.skipIf(asRoot)("lists a folder it cannot read when the user owns it or can write it, and skips one it can do neither with", async () => {
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    const dropBox = join(folder, "drop");
    mkdirSync(dropBox);
    chmodSync(dropBox, 0o333);
    const somebodyElse = (process.getuid?.() ?? 0) + 1;
    try {
      expect((await scanHooks(folder)).unreadable.sort()).toEqual([dropBox, locked]);
      // As though both were somebody else's: only the one a command can write into counts.
      expect((await scanHooks(folder, somebodyElse)).unreadable).toEqual([dropBox]);
    } finally {
      chmodSync(locked, 0o755);
      chmodSync(dropBox, 0o755);
    }
  });
});

describe("neutralizing", () => {
  it("makes each new hook non-executable, and leaves the user's own and samples", async () => {
    const own = hook(".git/hooks/pre-push");
    const baseline = (await scanHooks(folder)).hooks;
    const added = hook("sub/.git/hooks/pre-commit");
    const sample = hook(".git/hooks/pre-commit.sample");
    const target = hook("tools/run.sh");
    const linked = join(folder, ".git/hooks/post-checkout");
    symlinkSync(target, linked);
    const changed = await neutralize(folder, await scanHooks(folder), baseline);
    expect(changed.sort()).toEqual([linked, added].sort());
    expect(executable(added)).toBe(false);
    expect(executable(target)).toBe(false);
    expect(executable(own)).toBe(true);
    expect(executable(sample)).toBe(true);
  });

  it("makes the target of a link into another folder a command can write non-executable", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const target = hook("run.sh", 0o755, other);
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    symlinkSync(target, join(folder, ".git", "hooks", "pre-commit"));
    expect(await neutralize(folder, await scanHooks(folder), baseline, [folder, other])).toHaveLength(1);
    expect(executable(target)).toBe(false);
  });

  it("closes the hooks folder of a link to a program no command wrote, and leaves the program", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const hooks = join(folder, "repo", ".git", "hooks");
    mkdirSync(hooks, { recursive: true });
    symlinkSync("/bin/sh", join(hooks, "post-checkout"));
    try {
      expect(await neutralize(folder, await scanHooks(folder), baseline)).toEqual([join(hooks, "post-checkout")]);
      // Git can reach no hook in a folder it cannot search.
      expect(statSync(hooks).mode & 0o111).toBe(0);
      expect(executable("/bin/sh")).toBe(true);
    } finally {
      chmodSync(hooks, 0o755);
    }
  });

  it("treats a hook of the user's that a command made executable as new", async () => {
    const own = hook(".git/hooks/pre-push", 0o644);
    const baseline = (await scanHooks(folder)).hooks;
    chmodSync(own, 0o755);
    expect(await neutralize(folder, await scanHooks(folder), baseline)).toEqual([own]);
    expect(executable(own)).toBe(false);
  });

  it("treats a linked hook of the user's whose script a command rewrote as new", async () => {
    const script = hook("scripts/pre-commit");
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    const linked = join(folder, ".git", "hooks", "pre-commit");
    symlinkSync("../../scripts/pre-commit", linked);
    const baseline = (await scanHooks(folder)).hooks;
    writeFileSync(script, "#!/bin/sh\necho changed\n");
    expect(await neutralize(folder, await scanHooks(folder), baseline)).toEqual([linked]);
    expect(executable(script)).toBe(false);
  });
});

describe("the guard", () => {
  it("adds a notice to the command's output naming the hooks it changed, once", async () => {
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toBeNull();
    hook(".git/hooks/pre-commit");
    expect(await guard.after(ran("done\n"))).toEqual(ran(`done\n\n${HOOKS_NOTICE}.git/hooks/pre-commit`));
    expect(await guard.after(ran("again"))).toEqual(ran("again"));
  });

  it("makes hooks non-executable after a command that did not answer ok, with nothing to add", async () => {
    const guard = new HookGuard(folder);
    await guard.refusal();
    const added = hook(".git/hooks/pre-commit");
    const cancelled = { error: { type: "cancelled", message: "stopped" } };
    expect(await guard.after(cancelled)).toEqual(cancelled);
    expect(executable(added)).toBe(false);
  });

  it.skipIf(asRoot)("refuses commands while a folder the user owns cannot be read, and goes on once it can", async () => {
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    try {
      const guard = new HookGuard(folder);
      expect(await guard.refusal()).toEqual({
        error: { type: "sandbox", message: expect.stringContaining("locked") },
      });
      chmodSync(locked, 0o755);
      expect(await guard.refusal()).toBeNull();
    } finally {
      chmodSync(locked, 0o755);
    }
  });

  it("refuses commands when looking through the folder takes too long", async () => {
    for (let i = 0; i < 300; i += 1) mkdirSync(join(folder, `d${i}`, "e"), { recursive: true });
    const guard = new HookGuard(folder, { timeoutMs: 0 });
    expect(await guard.refusal()).toEqual({
      error: { type: "sandbox", message: expect.stringContaining("within 0 seconds") },
    });
  });

  it("tells the baseline it finds, once", async () => {
    const own = hook(".git/hooks/pre-push");
    const told: Array<ReadonlyMap<string, string>> = [];
    const guard = new HookGuard(folder, { known: (hooks) => told.push(hooks) });
    await guard.refusal();
    await guard.after(ran(""));
    expect(told.map((hooks) => [...hooks.keys()])).toEqual([[own]]);
  });

  it("refuses commands until it can record its baseline", async () => {
    let fail = true;
    const guard = new HookGuard(folder, {
      known: () => {
        if (fail) throw new Error("disk full");
      },
    });
    expect(await guard.refusal()).toEqual({ error: { type: "sandbox", message: expect.stringContaining("disk full") } });
    fail = false;
    expect(await guard.refusal()).toBeNull();
  });

  it("after a crash, makes what the killed host's commands left non-executable before any command", async () => {
    const own = hook(".git/hooks/pre-push");
    const inherited = (await scanHooks(folder)).hooks;
    const left = hook("sub/.git/hooks/pre-commit");
    const told: Array<ReadonlyMap<string, string>> = [];
    const guard = new HookGuard(folder, { inherited, known: (hooks) => told.push(hooks) });
    expect(await guard.refusal()).toBeNull();
    expect(executable(left)).toBe(false);
    expect(executable(own)).toBe(true);
    expect(told).toEqual([inherited]);
  });

  it("makes what a stopped command left non-executable when it settles, and says whether it saw everything", async () => {
    const guard = new HookGuard(folder);
    await guard.refusal();
    const left = hook(".git/hooks/pre-commit");
    expect(await guard.settle()).toBe(true);
    expect(executable(left)).toBe(false);
  });

  it.skipIf(asRoot)("settles as unclean when the last look could not see the whole folder", async () => {
    const guard = new HookGuard(folder);
    await guard.refusal();
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0);
    try {
      expect(await guard.settle()).toBe(false);
    } finally {
      chmodSync(locked, 0o755);
    }
  });
});
