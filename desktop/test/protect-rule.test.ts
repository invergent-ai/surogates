import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

// The oracle is protect.ts itself, as the source has it: a change to it shows here; and, for the names a kernel's
// line can add, explorer-names.ts.
import { EXPLORER_EXTENSIONS, EXPLORER_NAMES, explorerActsOn } from "../src/files/explorer-names.js";
import {
  DEPENDENCY_FOLDERS, folded, GIT_CONFIGS, GIT_STATE, KEY_FOLDERS, movesOutOfDependency, PROTECTED_NAMES, PROTECTED_PAIRS, protectedInFolder,
} from "../src/files/protect.js";

const SG_WALK = 12; // rule-match.h: the components judged, from the target up
const onHost = (p: string) => protectedInFolder("/f", `/f/${p}`);
const lower = (p: string) => folded(p).split("/");
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

// Every path of up to four components over the names protect.ts acts on, and a filler; and,
// deeper, every path of up to six over the names whose place counts, as a submodule's hooks do.
const NAMES = [...new Set([...PROTECTED_NAMES, ...PROTECTED_PAIRS.flat(), ...GIT_CONFIGS, ...GIT_STATE, "modules", "node_modules", "x"])];
function corpus(depth: number, names = NAMES): string[] {
  let all: string[] = [];
  let level = [""];
  for (let d = 0; d < depth; d++) {
    level = level.flatMap((p) => names.map((name) => (p ? `${p}/${name}` : name)));
    all = all.concat(level);
  }
  return all;
}
const CORPUS = [...corpus(4), ...corpus(6, [".git", "modules", "hooks", "config", "worktrees", "node_modules", ".vscode", "x"])];

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
  // A dependency folder: what packages ship goes in, git's names still do not.
  "node_modules/iconv-lite/.idea/codeStyles/Project.xml", ".venv/lib/python3.12/site-packages/pkg/.vscode/settings.json",
  "usr/lib/python3/dist-packages/pkg/.mcp.json", "node_modules/pkg/.claude/commands/x.md", "Node_Modules/pkg/.bashrc",
  "a/node_modules/b/node_modules/c/.idea/x", "node_modules/pkg/.gitmodules", "site-packages/pkg/.gitconfig",
  "node_modules/pkg/.git", "node_modules/pkg/.git/hooks/pre-commit", "node_modules/pkg/.git/config",
  ".vscode/node_modules/x", ".claude/commands/node_modules/x", "node_modules_old/.idea", "x/site-packages.bak/.vscode",
];
for (const name of DEPENDENCY_FOLDERS) probes.push(`${name}/pkg/.vscode/settings.json`, `${name}/.git/hooks/x`);
// Names a volume that compares names without case takes as protected ones (APFS by default, exFAT, an ext4
// casefold folder): ſ (U+017F) as s, ı (U+0131) and İ (U+0130) as i, the Kelvin sign (U+212A) as k, ß and ẞ
// as ss, and the ligatures U+FB00 to U+FB06 as the letters they join. Each refused by both.
const ALIASES = [
  ".git/hookſ/pre-commit", ".vſcode/settings.json", ".git/hooKs/pre-commit", ".gıt/config", ".Gİt/HOOKS/pre-commit", ".git/conﬁg",
  ".git/config.worKtree", ".claude/commandſ/x", ".claude/agentſ/x", ".bash_proﬁle", ".ripgreprc".replace("i", "ı"), "sub/.gıtmodules",
  ".git/modules/foo/hookſ/x", ".git/worktrees/wt/conﬁg", "a/.ıdea/x", ".mcp.jſon",
];
// And their neighbours that hold no protected name, folded or not, which both allow: a folded dependency folder
// is one, with what packages ship in it.
const NOT_ALIASES = ["hookſ/pre-commit", "ſrc/main.c", ".git/refs/heads/hookſ", "node_moduleſ/pkg/.idea/x", "diſt-packages/pkg/.vscode/x", "ﬀ/ﬃ/ﬄ/ﬂ", "ßtraße/ẞ", "K"];
// Paths protect.ts refuses only for git's transient state, which the rule allows.
const GIT_STATE_ONLY = [
  ".git/rebase-merge/git-rebase-todo", ".git/sequencer/todo", ".git/rebase-apply/0001",
  ".git/worktrees/wt/HEAD", ".git/modules/foo/rebase-merge/todo",
];
// The walk's named ceiling: a protected name more than SG_WALK components above the target.
const numbered = (n: number) => Array.from({ length: n }, (_, i) => String(i + 1));
const WITHIN = [".vscode", ...numbered(SG_WALK - 2), "x"].join("/");
const BEYOND = [".vscode", ...numbered(SG_WALK - 1), "x"].join("/");
// And a dependency folder past it, above such a name: the rule cannot see it, so it refuses.
const DEP_WITHIN = ["node_modules", ...numbered(SG_WALK - 3), ".vscode", "x"].join("/");
const DEP_BEYOND = ["node_modules", ...numbered(SG_WALK - 2), ".vscode", "x"].join("/");
// Directories moved (rename), refused and allowed.
const MOVED_REFUSED = [
  ".claude", "sub/.CLAUDE", ".git", ".git/modules", ".git/modules/foo", ".git/modules/a/b",
  ".git/modules/foo/refs/heads", ".git/worktrees", ".git/worktrees/wt", "sub/.git/worktrees/wt",
];
// Every pair of directory paths of up to three components over the dependency folders, a key and
// a filler, moved or exchanged; and, past the walk, a directory whose dependency folder the rule
// cannot see, which it lets go though protect.ts would not: nothing below that bound can hold a
// name sg_end would have let through.
const PAIR_PATHS = corpus(3, [...DEPENDENCY_FOLDERS, "x", ".vscode", ".claude", "p"]);
const PAIRS = PAIR_PATHS.flatMap((from) => PAIR_PATHS.map((to) => [from, to] as const));
const DEP_PAST = [["node_modules", ...numbered(SG_WALK), "d"].join("/"), "d"] as const;
const MOVED_ALLOWED = [".git/refs/heads", ".git/rebase-merge", ".git/objects/ab", ".git/info", ".claude/skills", "src", "modules/foo", "worktrees/wt"];

// The names Windows's Explorer acts on, each as it is, in other cases, and folded from the characters a volume that
// compares names without case may take to its letters; after names of every length about where the rule's read
// of a name's end begins (SG_TAIL, 24 bytes), and of characters of one to three bytes, which it may begin within;
// beside their near misses, which both allow; and in a folder, and as a folder.
const EXPLORER_SUFFIXES = [
  ...EXPLORER_NAMES, ...EXPLORER_EXTENSIONS, "DESKTOP.INI", "Desktop.ini", "AutoRun.INF", ".LNK", ".Url", ".SCF", ".Library-MS", ".SEARCHCONNECTOR-MS",
  "deſktop.ini", "desktop.ını", "desktop.İni", "de\u017Fk\u0131top.ini".replace("\u0131t", "t"), "desktop.ini".replace("k", "\u212A"), "autorun.ınf", ".ln\u212A",
  ".ſcf", ".lıbrary-mſ", ".ſearchconnector-mſ", ".ſearchconnector-ms",
  "desktop.ini.txt", "desktop.in", "esktop.ini", "desktop.inix", ".lnk2", ".lnkx", "lnk", ".ur", ".library", ".library-m", ".searchconnector", ".lnk.bak", "autorun.inf.old",
  ".ﬂk", "desktop.ıni.", ".scf ", ".lnk:stream",
];
const EXPLORER_PREFIXES = [
  "", "x", "a".repeat(5), "a".repeat(13), "a".repeat(19), "a".repeat(20), "a".repeat(21), "a".repeat(40), "a".repeat(200),
  "é".repeat(3), "é".repeat(12), "€".repeat(2), "€".repeat(9), "ß".repeat(10), "\u212A".repeat(8), "ﬃ".repeat(8), "ſ".repeat(11),
];
const EXPLORER_CORPUS = [
  ...EXPLORER_PREFIXES.flatMap((prefix) => EXPLORER_SUFFIXES.map((suffix) => `${prefix}${suffix}`)),
  "a/b/desktop.ini", "desktop.ini/x", "x.lnk/notes.txt", ".git/x.lnk", "node_modules/pkg/x.url", ".vscode/desktop.ini", "sub/Autorun.inf",
];

function pairRefuses(from: string, to: string, exchange: boolean): boolean {
  return movedRefuses(from) || movedRefuses(to) || movesOutOfDependency("/f", `/f/${from}`, `/f/${to}`, exchange);
}

function verdicts(bin: string, paths: string[], args: string[] = []): boolean[] {
  const out = execFileSync(bin, args, { input: `${paths.join("\n")}\n`, encoding: "utf8", maxBuffer: 1 << 26 }).split("\n");
  out.pop();
  expect(out.length).toBe(paths.length);
  return out.map((v) => v === "1");
}

// Each test here is CPU work of a fixed size and times nothing: the rule's verdict and protect.ts's on each of
// some 775,000 paths. The first takes about 4 s alone, and was measured at up to 15 s where other work shares
// the computer's cores, as a full suite's lanes do: past vitest's 10 s default, which is a bound on a hang and
// not on this work. So the bound here is a hang's still, four times the longest measured.
const VERDICTS_MS = 60_000;

describe("the guest rule mirrors protect.ts", { timeout: VERDICTS_MS }, () => {
  const cc = ["cc", "clang", "gcc"].find((c) => { try { execFileSync(c, ["--version"]); return true; } catch { return false; } });
  // Made with the harness: vitest runs no afterAll for a suite whose tests are all filtered out or skipped.
  let dir: string | undefined;
  afterAll(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
  });
  let bin: string | undefined;
  // Without a compiler every test here fails, on a developer's machine as on CI: skipped, they would
  // show nothing, and a name protect.ts gained that the rule lacks would pass unseen.
  function harness(): string {
    if (!cc) {
      throw new Error(
        "The guest rule's drift test needs a C compiler, and none of cc, clang or gcc runs on this PATH. It builds vm/rule-match.h " +
        "to compare the names the rule protects with those of files/protect.ts, which go uncompared without one. Install one " +
        "(build-essential has cc) and run this test again.",
      );
    }
    if (!bin) {
      dir = mkdtempSync(join(tmpdir(), "rule-"));
      bin = join(dir, "rule");
      execFileSync(cc, ["-O2", "-I", fileURLToPath(new URL("../vm/", import.meta.url)),
        "-o", bin, fileURLToPath(new URL("./rule-harness.c", import.meta.url))]);
    }
    return bin;
  }

  it("agrees on every path but git's transient state", () => {
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
    expect(verdicts(harness(), [DEP_WITHIN, DEP_BEYOND])).toEqual([false, true]);
    expect([onHost(DEP_WITHIN), onHost(DEP_BEYOND)]).toEqual([false, false]);
  });

  it("folds each name as protect.ts folds it", () => {
    const SG_NAMELEN = 16; // rule-match.h: the bytes of a folded name it compares
    const names = [
      "ſ", "ı", "İ", "ß", "ẞ", "\u212A", "ﬀ", "ﬁ", "ﬂ", "ﬃ", "ﬄ", "ﬅ", "ﬆ", "HOOKS", ".GİT", "conﬁg.wor\u212Atree", "ẞſßı", "Straße",
      "x".repeat(SG_NAMELEN), "ı".repeat(15), "\u212A".repeat(10), "x".repeat(31), "x".repeat(40), "é", "ÉCOLE",
    ];
    const out = execFileSync(harness(), ["fold"], { input: `${names.join("\n")}\n`, encoding: "utf8" }).split("\n").slice(0, -1);
    const ascii = (name: string) => /^[\x00-\x7f]*$/.test(name);
    // An ASCII name folded, its length and the bytes compared; one read only in part, its own length. The rule lower-cases
    // ASCII alone, and protect.ts all of a name: no other character's lower case is ASCII, so either folds it to no ASCII name.
    const want = (name: string) => {
      if (Buffer.byteLength(name) >= 32) return [name, String(Buffer.byteLength(name))];
      const fold = folded(name);
      return ascii(fold) ? [name, `${fold.length}\t${fold.slice(0, SG_NAMELEN)}`] : [name, "not ASCII"];
    };
    const got = (line: string, name: string) => {
      const [length = "", bytes = ""] = line.split("\t");
      if (Buffer.byteLength(name) >= 32) return [name, length];
      return [name, ascii(bytes) ? `${length}\t${bytes}` : "not ASCII"];
    };
    expect(out.map((line, i) => got(line, names[i]!))).toEqual(names.map(want));
  });

  it("refuses each name a volume that compares names without case takes as a protected one, as protect.ts does", () => {
    const paths = [...ALIASES, ...NOT_ALIASES];
    const kernel = verdicts(harness(), paths);
    expect(paths.map((p, i) => [p, kernel[i], onHost(p)]))
      .toEqual(paths.map((p) => [p, ALIASES.includes(p), ALIASES.includes(p)]));
    // A directory moved out of a folded dependency folder carries what was unpacked there too.
    expect(verdicts(harness(), ["node_moduleſ/p\tplanted", "diſt-packages/p\tſite-packages/p"], ["rename"])).toEqual([true, false]);
    expect([movesOutOfDependency("/f", "/f/node_moduleſ/p", "/f/planted"), movesOutOfDependency("/f", "/f/diſt-packages/p", "/f/ſite-packages/p")]).toEqual([true, false]);
    // A name too long to be read whole is too long to fold to a protected one: the longest that can is 18 bytes.
    expect(verdicts(harness(), [`.git/${"x".repeat(40)}`, `.git/hooKs`])).toEqual([false, true]);
  });

  it("refuses the names Windows's Explorer acts on where the kernel's line names that set, and with posix's decides as it does without one", () => {
    const paths = [...CORPUS, ...probes, ...GIT_STATE_ONLY, ...ALIASES, ...NOT_ALIASES, ...EXPLORER_CORPUS];
    const windows = verdicts(harness(), paths, ["windows"]);
    const bad = paths.flatMap((p, i) => {
      const want = ruleRefuses(p) || explorerActsOn(p);
      return windows[i] === want ? [] : [`${p}: rule=${windows[i]} expected=${want}`];
    });
    expect(bad).toEqual([]);
    // posix's, as Linux's and macOS's lines name it: the rule's own decisions alone, the Explorer's names among them.
    const posix = verdicts(harness(), EXPLORER_CORPUS);
    expect(EXPLORER_CORPUS.flatMap((p, i) => (posix[i] === ruleRefuses(p) ? [] : [`${p}: rule=${posix[i]} expected=${ruleRefuses(p)}`]))).toEqual([]);
    // The corpus holds both answers, at both ends of the read of a name's end.
    expect(EXPLORER_CORPUS.filter(explorerActsOn).length).toBeGreaterThan(250);
    expect(EXPLORER_CORPUS.filter((p) => !explorerActsOn(p) && !ruleRefuses(p)).length).toBeGreaterThan(300);
    expect(verdicts(harness(), [`${"a".repeat(200)}.searchconnector-ms`, `${"€".repeat(9)}.ſearchconnector-mſ`, "desktop.ini", `${"a".repeat(13)}desktop.ini`], ["windows"]))
      .toEqual([true, true, true, false]);
  });

  it("folds a name's end as protect.ts folds the name: all of one read whole, and of a longer one its last 24 bytes", () => {
    const names = ["Desktop.INI", "ﬁle.LNK", "deſ\u212Atop.ını", "ẞtraße.url", "x", `${"a".repeat(40)}.Library-MS`, `${"€".repeat(9)}.ſearchconnector-mſ`];
    const out = execFileSync(harness(), ["tail"], { input: `${names.join("\n")}\n` }).toString("latin1").split("\n").slice(0, -1);
    expect(out.slice(0, 5)).toEqual(names.slice(0, 5).map((name) => `${folded(name).length}\t${folded(name)}`));
    expect(out[5]).toBe(`24\t${"a".repeat(13)}.library-ms`);
    // Its end begins at the last of the nine euro signs, whose three bytes are none of a letter's.
    expect(out[6]).toBe(`22\t${Buffer.from("€").toString("latin1")}.searchconnector-ms`);
  });

  it("refuses moving a directory to or from a key folder's place", () => {
    const paths = [...CORPUS, ...MOVED_REFUSED, ...MOVED_ALLOWED];
    const moved = verdicts(harness(), paths, ["dir"]);
    const bad = paths.flatMap((p, i) => (moved[i] === movedRefuses(p) ? [] : [`${p}: rule=${moved[i]} expected=${movedRefuses(p)}`]));
    expect(bad).toEqual([]);
    expect(verdicts(harness(), [...MOVED_REFUSED, ...MOVED_ALLOWED], ["dir"]))
      .toEqual([...MOVED_REFUSED.map(() => true), ...MOVED_ALLOWED.map(() => false)]);
  });

  it("refuses a directory moved out of a dependency folder, or exchanged with one in one", () => {
    for (const exchange of [false, true]) {
      const kernel = verdicts(harness(), PAIRS.map(([from, to]) => `${from}\t${to}`), [exchange ? "exchange" : "rename"]);
      const bad = PAIRS.flatMap(([from, to], i) => {
        const want = pairRefuses(from, to, exchange);
        return kernel[i] === want ? [] : [`${from} -> ${to} (exchange: ${exchange}): rule=${kernel[i]} expected=${want}`];
      });
      expect(bad).toEqual([]);
    }
    expect(verdicts(harness(), ["node_modules/p\tplanted", "node_modules/p\tnode_modules/.p-old", "plain\tnode_modules/plain"], ["rename"]))
      .toEqual([true, false, false]);
    expect(verdicts(harness(), [DEP_PAST.join("\t")], ["rename"])).toEqual([false]);
    expect(movesOutOfDependency("/f", `/f/${DEP_PAST[0]}`, `/f/${DEP_PAST[1]}`)).toBe(true);
  });
});
