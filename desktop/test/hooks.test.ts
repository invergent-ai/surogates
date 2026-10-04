import { chmodSync, linkSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { HOOKS_NOTICE, HookGuard, MAX_LISTED, chmodInside, isGitHook, neutralize, scanHooks } from "../src/hosts/hooks.js";

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

  it("walks a submodule named objects even when a command wrote a HEAD into the folder above it", async () => {
    mkdirSync(join(folder, ".git", "modules", "objects"), { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(folder, ".git", "modules", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(folder, ".git", "modules", "objects", "HEAD"), "ref: refs/heads/main\n");
    const inner = hook(".git/modules/objects/hooks/pre-commit");
    expect([...(await scanHooks(folder)).hooks.keys()]).toEqual([inner]);
  });

  it.skipIf(asRoot)("walks a git folder's objects folder it cannot search, as it may be a submodule's git folder", async () => {
    const objects = join(folder, ".git", "modules", "objects");
    mkdirSync(objects, { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(folder, ".git", "modules", "HEAD"), "ref: refs/heads/main\n");
    writeFileSync(join(objects, "HEAD"), "ref: refs/heads/main\n");
    hook(".git/modules/objects/hooks/pre-commit");
    chmodSync(objects, 0o600);
    try {
      expect((await scanHooks(folder)).unreadable).toEqual([join(objects, "hooks")]);
    } finally {
      chmodSync(objects, 0o755);
    }
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

  it.skipIf(asRoot)("lists a folder below a hooks folder it cannot read only while a hook could still run a file in it", async () => {
    const hooks = join(folder, ".git", "hooks");
    // A hook can run a file in a folder it can search but not list.
    const searchable = join(hooks, "pre-commit.d");
    const closed = join(hooks, "post-checkout.d");
    mkdirSync(searchable, { recursive: true });
    mkdirSync(closed);
    chmodSync(searchable, 0o311);
    chmodSync(closed, 0);
    try {
      expect((await scanHooks(folder)).unreadable).toEqual([searchable]);
    } finally {
      chmodSync(searchable, 0o755);
      chmodSync(closed, 0o755);
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
    const { changed } = await neutralize(folder, await scanHooks(folder), baseline);
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
    expect((await neutralize(folder, await scanHooks(folder), baseline, [folder, other])).changed).toHaveLength(1);
    expect(executable(target)).toBe(false);
  });

  it("closes the hooks folder of a link to a program no command wrote, and leaves the program", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const hooks = join(folder, "repo", ".git", "hooks");
    mkdirSync(hooks, { recursive: true });
    symlinkSync("/bin/sh", join(hooks, "post-checkout"));
    try {
      expect((await neutralize(folder, await scanHooks(folder), baseline)).changed).toEqual([join(hooks, "post-checkout")]);
      // Git can reach no hook in a folder it cannot search.
      expect(statSync(hooks).mode & 0o111).toBe(0);
      expect(executable("/bin/sh")).toBe(true);
    } finally {
      chmodSync(hooks, 0o755);
    }
  });

  it("closes the hooks folder of a hook hard-linked to a file elsewhere, and leaves the file", async () => {
    const baseline = (await scanHooks(folder)).hooks;
    const target = hook("run.sh", 0o755, other);
    const hooks = join(folder, ".git", "hooks");
    mkdirSync(hooks, { recursive: true });
    linkSync(target, join(hooks, "pre-commit"));
    try {
      const { changed } = await neutralize(folder, await scanHooks(folder), baseline, [folder, other]);
      // A chmod would change the file at its other paths too.
      expect(executable(target)).toBe(true);
      expect(statSync(hooks).mode & 0o111).toBe(0);
      expect(changed).toEqual([join(hooks, "pre-commit")]);
    } finally {
      chmodSync(hooks, 0o755);
    }
  });

  it("treats a hook of the user's that a command made executable as new", async () => {
    const own = hook(".git/hooks/pre-push", 0o644);
    const baseline = (await scanHooks(folder)).hooks;
    chmodSync(own, 0o755);
    expect((await neutralize(folder, await scanHooks(folder), baseline)).changed).toEqual([own]);
    expect(executable(own)).toBe(false);
  });

  it("treats a linked hook of the user's whose script a command rewrote as new", async () => {
    const script = hook("scripts/pre-commit");
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    const linked = join(folder, ".git", "hooks", "pre-commit");
    symlinkSync("../../scripts/pre-commit", linked);
    const baseline = (await scanHooks(folder)).hooks;
    writeFileSync(script, "#!/bin/sh\necho changed\n");
    expect((await neutralize(folder, await scanHooks(folder), baseline)).changed).toEqual([linked]);
    expect(executable(script)).toBe(false);
  });
});

describe("a mode change", () => {
  const closed = (mode: number) => mode & 0o7666;

  it("is made where the opened file really is, so a link on its path cannot lead it out of the folder", async () => {
    const outside = hook("run.sh", 0o755, other);
    symlinkSync(other, join(folder, "d"));
    expect(await chmodInside(join(folder, "d", "run.sh"), [folder], closed)).toBe(false);
    // A link as the last part is never followed, though it leads inside.
    const inner = hook("real.sh");
    symlinkSync(inner, join(folder, "link.sh"));
    expect(await chmodInside(join(folder, "link.sh"), [folder], closed)).toBe(false);
    expect(statSync(outside).mode & 0o777).toBe(0o755);
    expect(executable(inner)).toBe(true);
  });

  it("is made on a file inside the folder", async () => {
    const inner = hook("run.sh");
    expect(await chmodInside(inner, [folder], closed)).toBe(true);
    expect(statSync(inner).mode & 0o777).toBe(0o644);
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

  it("tells the hooks a look between commands stopped with the next command's output", async () => {
    const guard = new HookGuard(folder);
    await guard.refusal();
    const added = hook(".git/hooks/pre-commit");
    await guard.watch();
    expect(executable(added)).toBe(false);
    expect(await guard.after(ran("next"))).toEqual(ran(`next\n${HOOKS_NOTICE}.git/hooks/pre-commit`));
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

  it("goes on past a folder below a hooks folder it closed, as git runs nothing from there", async () => {
    const hooks = join(folder, ".git", "hooks");
    mkdirSync(join(hooks, "pre-commit.d"), { recursive: true });
    const guard = new HookGuard(folder);
    await guard.refusal();
    symlinkSync("/bin/sh", join(hooks, "post-checkout"));
    try {
      await guard.after(ran(""));
      expect(statSync(hooks).mode & 0o111).toBe(0);
      expect(await guard.settle()).toBe(true);
      expect(await guard.refusal()).toBeNull();
    } finally {
      chmodSync(hooks, 0o755);
    }
  });

  it.skipIf(asRoot)("names at most a few of the folders it cannot read", async () => {
    const locked = Array.from({ length: MAX_LISTED + 1 }, (_, i) => join(folder, `locked${String(i).padStart(2, "0")}`));
    for (const dir of locked) {
      mkdirSync(dir);
      chmodSync(dir, 0);
    }
    try {
      expect(await new HookGuard(folder).refusal()).toEqual({
        error: { type: "sandbox", message: expect.stringContaining(`${basename(locked[MAX_LISTED - 1] ?? "")} and 1 more in this folder`) },
      });
    } finally {
      for (const dir of locked) chmodSync(dir, 0o755);
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

describe("the walk's protected keys", () => {
  it("are every entry under a protected name at any depth, folders too, and a .git file, but not a .git folder", async () => {
    const deep = hook("a/b/c/d/e/f/g/h/i/j/k/.git/hooks/pre-commit");
    mkdirSync(join(folder, ".vscode"));
    writeFileSync(join(folder, ".vscode", "settings.json"), "{}");
    mkdirSync(join(folder, ".git"));
    writeFileSync(join(folder, ".git", "config"), "");
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    mkdirSync(join(folder, "wt"));
    writeFileSync(join(folder, "wt", ".git"), "gitdir: /elsewhere\n");
    writeFileSync(join(folder, "README.md"), "");
    expect([...(await scanHooks(folder)).protectedKeys].sort()).toEqual([
      join(folder, ".git", "config"),
      join(folder, ".vscode"),
      join(folder, ".vscode", "settings.json"),
      join(folder, "a/b/c/d/e/f/g/h/i/j/k/.git/hooks"),
      deep,
      join(folder, "wt", ".git"),
    ].sort());
  });

  it("leave out node_modules and a git folder's object store", async () => {
    mkdirSync(join(folder, "node_modules", "pkg", ".vscode"), { recursive: true });
    writeFileSync(join(folder, "node_modules", "pkg", ".vscode", "settings.json"), "{}");
    mkdirSync(join(folder, ".git", "objects", ".vscode"), { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
    expect([...(await scanHooks(folder)).protectedKeys]).toEqual([]);
  });

  it("are told for every look, with when it started", async () => {
    const told: Array<{ keys: string[]; startedAt: number }> = [];
    const guard = new HookGuard(folder, { seen: (keys, startedAt) => told.push({ keys: [...keys], startedAt }) });
    await guard.refusal();
    const before = performance.now();
    writeFileSync(join(folder, ".mcp.json"), "{}");
    await guard.watch();
    expect(told.map((look) => look.keys)).toEqual([[], [join(folder, ".mcp.json")]]);
    expect(told[1]?.startedAt).toBeGreaterThanOrEqual(before);
  });

  it("are not told for a look that timed out, and take a .git link as a key", async () => {
    for (let i = 0; i < 300; i += 1) mkdirSync(join(folder, `d${i}`, "e"), { recursive: true });
    const told: unknown[] = [];
    await new HookGuard(folder, { timeoutMs: 0, seen: (keys) => told.push(keys) }).refusal();
    expect(told).toEqual([]);
    symlinkSync("/elsewhere", join(folder, "d0", ".git"));
    expect([...(await scanHooks(folder)).protectedKeys]).toEqual([join(folder, "d0", ".git")]);
  });
});
