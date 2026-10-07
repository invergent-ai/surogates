import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// A faithful port of desktop/src/files/protect.ts protectedInFolder (the oracle).
const PN = new Set([".gitconfig",".gitmodules",".bashrc",".bash_profile",".zshrc",".zprofile",".profile",".ripgreprc",".mcp.json",".vscode",".idea"]);
const PAIRS: [string,string][] = [[".claude","commands"],[".claude","agents"],[".git","hooks"],[".git","config"]];
const GIT_STATE = new Set(["worktrees","rebase-merge","rebase-apply","sequencer"]);
const GIT_CONFIGS = new Set(["config","config.worktree","commondir"]);
function runsCode(rest: string[]): boolean {
  const [first, ...after] = rest;
  if (first === "modules" && after.length > 1) {
    const below = after.slice(1);
    return below.some((p) => p === "hooks" || GIT_STATE.has(p)) || GIT_CONFIGS.has(below.at(-1) ?? "");
  }
  return first !== undefined && (GIT_STATE.has(first) || first === "hooks" || (rest.length === 1 && GIT_CONFIGS.has(first)));
}
function protectedInFolder(rel: string): boolean {
  const parts = rel.toLowerCase().split("/");
  return parts.at(-1) === ".git" || parts.some((part, i) =>
    PN.has(part) || PAIRS.some(([a, b]) => part === a && parts[i + 1] === b) || (part === ".git" && runsCode(parts.slice(i + 1))));
}

// Paths protect.ts refuses only because of git transient-state content the kernel
// deliberately allows (decision 3); the kernel allows exactly these.
const GIT_STATE_ONLY = new Set([
  ".git/rebase-merge/git-rebase-todo", ".git/sequencer/todo", ".git/rebase-apply/0001",
  ".git/worktrees/wt/HEAD", ".git/modules/foo/rebase-merge/todo",
]);

const probes = [
  ".git/hooks/pre-commit", ".git/config", ".git/config.worktree", ".git/commondir", ".git",
  ".git/modules/foo/config", ".git/modules/foo/hooks/pre-commit",
  ".git/worktrees/wt/commondir", ".git/worktrees/wt/config.worktree",
  ".git/refs/heads/hooks", ".git/refs/heads/config", ".git/logs/refs/heads/hooks",
  ".git/rebase-merge/git-rebase-todo", ".git/sequencer/todo", ".git/rebase-apply/0001",
  ".mcp.json", "a/b/c/.vscode/settings.json", ".claude/commands/x", ".claude/agents/y",
  "sub/.git/config", "sub/.git", "notes.txt", ".git/refs/heads/tmp", "src/main.c",
  ".gitconfig", ".gitmodules", ".bashrc", ".idea/x",
  ".git/worktrees/wt/HEAD", ".git/modules/a/b/hooks/x", ".git/modules/foo/rebase-merge/todo",
  "deep/a/b/c/d/.git/config", ".git/objects/ab/cd", ".git/HEAD", ".git/index",
];

describe("the guest rule mirrors protect.ts", () => {
  const cc = ["cc", "clang", "gcc"].find((c) => { try { execFileSync(c, ["--version"]); return true; } catch { return false; } });
  it.skipIf(!cc)("agrees on every probe but the pinned git-state allows", () => {
    const dir = mkdtempSync(join(tmpdir(), "rule-"));
    const bin = join(dir, "rule");
    execFileSync(cc!, ["-O2", "-I", new URL("../vm/", import.meta.url).pathname,
      "-o", bin, new URL("./rule-harness.c", import.meta.url).pathname]);
    const out = execFileSync(bin, probes, { encoding: "utf8" }).trim().split("\n");
    const kernel = new Map(out.map((l) => { const [v, ...p] = l.split(" "); return [p.join(" "), v === "1"]; }));
    const bad: string[] = [];
    for (const p of probes) {
      const h = protectedInFolder(p), k = kernel.get(p);
      if (h === k) continue;
      if (h && !k && GIT_STATE_ONLY.has(p)) continue; // intended (decision 3)
      bad.push(`${p}: protect.ts=${h} kernel=${k}`);
    }
    expect(bad).toEqual([]);
  });
});
