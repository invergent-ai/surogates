import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

// The oracle is protect.ts itself, as built (npm run build): a change to it shows here.
const { GIT_CONFIGS, GIT_STATE, KEY_FOLDERS, PROTECTED_NAMES, PROTECTED_PAIRS, protectedInFolder } =
  (await import(new URL("../dist/files/protect.js", import.meta.url).href)) as typeof import("../src/files/protect.js");

const SG_WALK = 12; // rule-match.h: the components judged, from the target up
const onHost = (p: string) => protectedInFolder("/f", `/f/${p}`);
const lower = (p: string) => p.toLowerCase().split("/");
const inGitState = (parts: string[]) => parts.some((part, i) => GIT_STATE.has(part) && parts.slice(0, i).includes(".git"));

// What the guest rule must answer: protect.ts's verdict, with git's transient state no
// reason to refuse (the guest's own git rebases and cherry-picks), except a linked
// worktree's config leaf (.git/worktrees/<name>/…/config), which redirects the host's git.
function ruleRefuses(p: string): boolean {
  const parts = lower(p);
  const worktreeConfig = GIT_CONFIGS.has(parts.at(-1) ?? "") &&
    parts.some((part, i) => part === ".git" && parts[i + 1] === "worktrees" && parts.length - i >= 4);
  return worktreeConfig || onHost(parts.map((part) => (GIT_STATE.has(part) ? "x" : part)).join("/"));
}

// A moved directory carries its whole tree, which neither of its paths names, so the rule
// also refuses one that takes or leaves a key folder's place (KEY_FOLDERS), or a place at
// or under .git/modules or .git/worktrees, where submodules' and worktrees' configs live.
function movedRefuses(p: string): boolean {
  const parts = lower(p);
  return ruleRefuses(p) || KEY_FOLDERS.has(parts.at(-1) ?? "") ||
    parts.some((part, i) => part === ".git" && (parts[i + 1] === "modules" || parts[i + 1] === "worktrees"));
}

// Every path of up to four components over the names protect.ts acts on, and a filler.
const NAMES = [...new Set([...PROTECTED_NAMES, ...PROTECTED_PAIRS.flat(), ...GIT_CONFIGS, ...GIT_STATE, "modules", "x"])];
function corpus(depth: number): string[] {
  let all: string[] = [];
  let level = [""];
  for (let d = 0; d < depth; d++) {
    level = level.flatMap((p) => NAMES.map((name) => (p ? `${p}/${name}` : name)));
    all = all.concat(level);
  }
  return all;
}
const CORPUS = corpus(4);

// The readable spec, beside the corpus.
const probes = [
  ".git/hooks/pre-commit", ".git/config", ".git/config.worktree", ".git/commondir", ".git",
  ".git/modules/foo/config", ".git/modules/foo/hooks/pre-commit", ".git/modules/hooks/HEAD",
  ".git/worktrees/wt/commondir", ".git/worktrees/wt/config.worktree",
  ".git/refs/heads/hooks", ".git/refs/heads/config", ".git/logs/refs/heads/hooks",
  ".mcp.json", "a/b/c/.vscode/settings.json", ".claude/commands/x", ".claude/agents/y",
  "sub/.git/config", "sub/.git", "notes.txt", ".git/refs/heads/tmp", "src/main.c",
  ".gitconfig", ".gitmodules", ".bashrc", ".idea/x",
  ".git/modules/a/b/hooks/x", "deep/a/b/c/d/.git/config", ".git/objects/ab/cd", ".git/HEAD", ".git/index",
  ".GIT/config", ".Git/Hooks/pre-commit", ".GIT/MODULES/Foo/CONFIG", "a/.CLAUDE/Commands/x",
];
// Paths protect.ts refuses only for git's transient state, which the rule allows.
const GIT_STATE_ONLY = [
  ".git/rebase-merge/git-rebase-todo", ".git/sequencer/todo", ".git/rebase-apply/0001",
  ".git/worktrees/wt/HEAD", ".git/modules/foo/rebase-merge/todo",
];
// The walk's named ceiling: a protected name more than SG_WALK components above the target.
const numbered = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));
const WITHIN = [".vscode", ...numbered(SG_WALK - 2), "x"].join("/");
const BEYOND = [".vscode", ...numbered(SG_WALK - 1), "x"].join("/");
// Directories moved (rename), refused and allowed.
const MOVED_REFUSED = [
  ".claude", "sub/.CLAUDE", ".git", ".git/modules", ".git/modules/foo", ".git/modules/a/b",
  ".git/modules/foo/refs/heads", ".git/worktrees", ".git/worktrees/wt", "sub/.git/worktrees/wt",
];
const MOVED_ALLOWED = [".git/refs/heads", ".git/rebase-merge", ".git/objects/ab", ".git/info", ".claude/skills", "src", "modules/foo", "worktrees/wt"];

function verdicts(bin: string, paths: string[], args: string[] = []): boolean[] {
  const out = execFileSync(bin, args, { input: `${paths.join("\n")}\n`, encoding: "utf8", maxBuffer: 1 << 26 }).split("\n");
  out.pop();
  expect(out.length).toBe(paths.length);
  return out.map((v) => v === "1");
}

describe("the guest rule mirrors protect.ts", () => {
  const cc = ["cc", "clang", "gcc"].find((c) => { try { execFileSync(c, ["--version"]); return true; } catch { return false; } });
  const dir = mkdtempSync(join(tmpdir(), "rule-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));
  let bin: string | undefined;
  function harness(): string {
    if (!cc) throw new Error("no C compiler (cc, clang or gcc) to build the guest rule's harness");
    if (!bin) {
      bin = join(dir, "rule");
      execFileSync(cc, ["-O2", "-I", fileURLToPath(new URL("../vm/", import.meta.url)),
        "-o", bin, fileURLToPath(new URL("./rule-harness.c", import.meta.url))]);
    }
    return bin;
  }
  // Without a compiler these are skipped on a developer's machine, and fail on CI.
  const withCc = it.skipIf(!cc && process.env.CI !== "true");

  withCc("agrees on every path but git's transient state", () => {
    const paths = [...CORPUS, ...probes, ...GIT_STATE_ONLY];
    const kernel = verdicts(harness(), paths);
    const bad: string[] = [];
    paths.forEach((p, i) => {
      const host = onHost(p);
      if (kernel[i] !== ruleRefuses(p)) bad.push(`${p}: rule=${kernel[i]} expected=${ruleRefuses(p)} protect.ts=${host}`);
      else if (kernel[i] !== host && (kernel[i] || !inGitState(lower(p)))) bad.push(`${p}: rule=${kernel[i]} protect.ts=${host}`);
    });
    expect(bad).toEqual([]);
    expect(GIT_STATE_ONLY.map((p) => [p, kernel[paths.indexOf(p)], onHost(p)])).toEqual(GIT_STATE_ONLY.map((p) => [p, false, true]));
    expect(verdicts(harness(), [WITHIN, BEYOND])).toEqual([true, false]);
    expect([onHost(WITHIN), onHost(BEYOND)]).toEqual([true, true]);
  });

  withCc("refuses moving a directory to or from a key folder's place", () => {
    const paths = [...CORPUS, ...MOVED_REFUSED, ...MOVED_ALLOWED];
    const moved = verdicts(harness(), paths, ["dir"]);
    const bad = paths.flatMap((p, i) => (moved[i] === movedRefuses(p) ? [] : [`${p}: rule=${moved[i]} expected=${movedRefuses(p)}`]));
    expect(bad).toEqual([]);
    expect(verdicts(harness(), [...MOVED_REFUSED, ...MOVED_ALLOWED], ["dir"]))
      .toEqual([...MOVED_REFUSED.map(() => true), ...MOVED_ALLOWED.map(() => false)]);
  });
});
