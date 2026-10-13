// Which files a search includes when the file host lists the tree itself (src/files/glob.ts, used by the Windows walk in
// fd-paths-win32.ts): held to ripgrep's own `-g` output, the oracle (C1's ruling). The matcher and the walk's hidden
// and own-file rules are replayed here over a tree, and compared with `rg --files` walking the same tree with the same
// globs. It needs ripgrep on PATH (every runner has it) and runs on every OS, since the matcher is pure.

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { descendsHidden, included, matchGlob } from "../src/files/glob.js";

const OWN = /^\.surogate-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.tmp$/;
const NOT_OWN = "!.surogate-????????-????-????-????-????????????.tmp";
const hasRg = (() => {
  try {
    execFileSync("rg", ["--version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
})();

// The tree the cross-check walks: code, dot-files and dot-folders, a dependency folder, and a helper's own temp file.
const FILES = [
  "a.txt", "b.log", "readme.md", "src/main.ts", "src/util.ts", "src/deep/a/b/d.py", "sub/c.txt", "sub/a.txt", "normal/y.txt",
  "node_modules/pkg/x.js", "node_modules/pkg/y.ts", "test_a.py", "test_b.py", ".env", ".hidden/x.txt", ".config/app.json",
  ".surogate-0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f.tmp", ".surogate-notes.tmp",
];

let root: string;
beforeAll(() => {
  if (!hasRg) return;
  root = mkdtempSync(join(tmpdir(), "glob-"));
  for (const file of FILES) {
    const at = join(root, file);
    mkdirSync(join(at, ".."), { recursive: true });
    writeFileSync(at, "HIT\n");
  }
});
afterAll(() => {
  if (root && existsSync(root)) rmSync(root, { recursive: true, force: true });
});

// The walk's file set for *glob*, as fd-paths-win32.ts chooses it: prune a hidden folder the glob does not whitelist,
// drop a hidden file unless a positive glob matches it, drop the helper's own, and apply the glob.
function walked(glob: string | null): string[] {
  const positive = glob !== null && !glob.startsWith("!");
  const out: string[] = [];
  const visit = (rel: string): void => {
    for (const [name, isDir] of readdir(join(root, rel))) {
      const r = rel ? `${rel}/${name}` : name;
      if (isDir) {
        if (name.startsWith(".") && !descendsHidden(`/${r}`, glob)) continue;
        visit(r);
      } else {
        if (OWN.test(name)) continue;
        if (name.startsWith(".") && !positive) continue;
        if (included(`/${r}`, glob)) out.push(r);
      }
    }
  };
  visit("");
  return out.sort();
}

// Node's readdir with types, through the test's own fs (not the walk, which needs Windows's ntat).
function readdir(dir: string): Array<[string, boolean]> {
  return readdirSync(dir, { withFileTypes: true }).map((e) => [e.name, e.isDirectory()] as [string, boolean]);
}

// rg's file set for *glob*, walking the folder with NOT_OWN last, as the file host passes it.
function rg(glob: string | null): string[] {
  const args = ["--no-ignore", "--files"];
  if (glob !== null) args.push("-g", glob);
  args.push("-g", NOT_OWN, root);
  let out = "";
  try {
    out = execFileSync("rg", args, { encoding: "utf8" });
  } catch (error) {
    out = (error as { stdout?: string }).stdout ?? "";
  }
  return out.split("\n").filter(Boolean).map((p) => p.slice(root.length + 1).replaceAll("\\", "/")).sort();
}

describe("the search's file set matches ripgrep's", () => {
  it.runIf(hasRg)("chooses the same files as rg --files for each glob", () => {
    const globs = [
      null, "*", "**", "*.txt", "*.ts", "*.py", "**/*.ts", "**/*.py", "src/**", "src/*.ts", "test_*", "*config*", "[ab].txt",
      "readme.md", "x.js", "pkg/*.js", "!*.log", "!node_modules", "!sub", "!*.ts", "!test_*", "src/deep/**/*.py",
    ];
    const bad = globs.filter((glob) => JSON.stringify(walked(glob)) !== JSON.stringify(rg(glob)))
      .map((glob) => `${JSON.stringify(glob)}: mine=${walked(glob)} rg=${rg(glob)}`);
    expect(bad).toEqual([]);
  });
});

describe("the glob matcher, on its own", () => {
  // The path is matched with a leading slash, as ripgrep matches the folder's absolute path: a glob with no slash matches
  // the name at any depth, and one with a slash anchors to the filesystem's root, so a glob the user anchors to the
  // search folder (src/**) matches nothing, exactly as it does for ripgrep given the folder's absolute path. Only a
  // leading ** reaches into the tree.
  it("matches a name at any depth without a slash, and takes ** across names, as ripgrep does from an absolute root", () => {
    expect(matchGlob("/src/a.ts", "*.ts")).toBe(true);
    expect(matchGlob("/src/a.ts", "a.ts")).toBe(true);
    expect(matchGlob("/src/a.ts", "src/*.ts")).toBe(false);
    expect(matchGlob("/src/deep/a.ts", "src/**")).toBe(false);
    expect(matchGlob("/src/deep/a.ts", "**/*.ts")).toBe(true);
    expect(matchGlob("/src/a.ts", "**/src/*.ts")).toBe(true);
    expect(matchGlob("/a.ts", "[ab].ts")).toBe(true);
    expect(matchGlob("/c.ts", "[!ab].ts")).toBe(true);
  });

  it("includes by a positive glob, and all but a negated glob and its subtree", () => {
    expect(included("/sub/c.txt", "!sub")).toBe(false);
    expect(included("/sub/c.txt", "!*.log")).toBe(true);
    expect(included("/b.log", "!*.log")).toBe(false);
    expect(included("/src/a.ts", null)).toBe(true);
    expect(included("/src/a.ts", "sub")).toBe(false);
  });
});
