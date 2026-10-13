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
const lower = (p: string) => p.split("/").map(folded);
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
// as ss, and the ligatures U+FB00 to U+FB06 as the letters they join. And those a volume takes as protected ones
// once it drops what they hold: HFS+ compares names without the code points its comparison ignores (Apple's
// TN1150), and FAT's and exFAT's volumes drop the dots and spaces a name ends with. Each refused by both.
const ALIASES = [
  ".git/hookſ/pre-commit", ".vſcode/settings.json", ".git/hooKs/pre-commit", ".gıt/config", ".Gİt/HOOKS/pre-commit", ".git/conﬁg",
  ".git/config.worKtree", ".claude/commandſ/x", ".claude/agentſ/x", ".bash_proﬁle", ".ripgreprc".replace("i", "ı"), "sub/.gıtmodules",
  ".git/modules/foo/hookſ/x", ".git/worktrees/wt/conﬁg", "a/.ıdea/x", ".mcp.jſon",
  ".git./hooks/x", ".git/ho\u200coks/pre-commit", ".vscode /settings.json", ".mcp.json.", "\ufeff.idea/x", ".git /config",
  ".claude/commands./x", ".git/hooks\u206a/x", ".bashrc \u202e", "sub/.gitmodules.", ".git\u200f\u202a\u202b\u202c\u202d/config",
  ".git/modules\u206f/foo/hooks./x", ".git/worktrees/wt/config. ", ".git.", ".Gıt\u200d. ./Hookſ./x", ".claude\u206b/agents\u206c/y",
  `.git/hooks${".".repeat(300)}/x`, `.git/ho${"\u200c".repeat(300)}oks/x`, `${"\ufeff".repeat(20)}.vscode${" ".repeat(40)}/x`,
];
// And their neighbours that hold no protected name, folded or not, which both allow: a folded dependency folder
// is one, with what packages ship in it. U+200B, U+2010, U+2029, U+202F, U+2069, U+2070 and U+FEFE lie beside
// the code points HFS+ ignores, and are kept; so is a dot or a space before a name's end.
const NOT_ALIASES = [
  "hookſ/pre-commit", "ſrc/main.c", ".git/refs/heads/hookſ", "node_moduleſ/pkg/.idea/x", "diſt-packages/pkg/.vscode/x", "ﬀ/ﬃ/ﬄ/ﬂ", "ßtraße/ẞ", "K",
  "node_modules./pkg/.idea/x", "site-packages\u200c/pkg/.mcp.json", ".git/ho\u200boks/x", ".git/\u2010hooks/x", ".git/hooks\u2029/x",
  ".git/\u202fhooks/x", ".git/hooks\u2069/x", ".git/\u2070hooks/x", "\ufefe.vscode/x", ".gi.t/hooks/x", ".git/refs/heads/x.", "notes. ",
  "hooks./x", ". .vscode/x", `${"x".repeat(40)}${".".repeat(40)}`,
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
// compares names without case may take to its letters; as a volume takes them once it drops what they hold, the code
// points HFS+ ignores anywhere and the dots and spaces they end with, however many; after names of every length about
// where the rule keeps a name's last letters (SG_ENDLEN, 24), and of characters of one to three bytes; beside their
// near misses; and in a folder, and as a folder. A trailing dot or space, and a ':', are forms NTFS may resolve to the
// name too, which the rule refuses under windows whatever the name (windowsAlias).
const EXPLORER_SUFFIXES = [
  ...EXPLORER_NAMES, ...EXPLORER_EXTENSIONS, "DESKTOP.INI", "Desktop.ini", "AutoRun.INF", ".LNK", ".Url", ".SCF", ".Library-MS", ".SEARCHCONNECTOR-MS",
  "deſktop.ini", "desktop.ını", "desktop.İni", "de\u017Fk\u0131top.ini".replace("\u0131t", "t"), "desktop.ini".replace("k", "\u212A"), "autorun.ınf", ".ln\u212A",
  ".ſcf", ".lıbrary-mſ", ".ſearchconnector-mſ", ".ſearchconnector-ms",
  `desktop.ini${"\u200c".repeat(12)}`, ".l\u200dnk", ".url\ufeff", `autorun.inf${"\u206a".repeat(30)}`, `.scf${". ".repeat(20)}`, "\ufeffdesktop.ini",
  "desktop.ini.txt", "desktop.in", "esktop.ini", "desktop.inix", ".lnk2", ".lnkx", "lnk", ".ur", ".library", ".library-m", ".searchconnector", ".lnk.bak", "autorun.inf.old",
  ".ﬂk", "desktop.ıni.", ".scf ", ".lnk:stream", ".ln\u200bk", "desktop\u200b.ini",
];
const EXPLORER_PREFIXES = [
  "", "x", "a".repeat(5), "a".repeat(13), "a".repeat(19), "a".repeat(20), "a".repeat(21), "a".repeat(40), "a".repeat(200),
  "é".repeat(3), "é".repeat(12), "€".repeat(2), "€".repeat(9), "ß".repeat(10), "\u212A".repeat(8), "ﬃ".repeat(8), "ſ".repeat(11),
];
const EXPLORER_CORPUS = [
  ...EXPLORER_PREFIXES.flatMap((prefix) => EXPLORER_SUFFIXES.map((suffix) => `${prefix}${suffix}`)),
  "a/b/desktop.ini", "desktop.ini/x", "x.lnk/notes.txt", ".git/x.lnk", "node_modules/pkg/x.url", ".vscode/desktop.ini", "sub/Autorun.inf",
];

// A component NTFS may resolve to another name, which the rule refuses under windows (rule-match.h, sg_windows_alias):
// a ':' or '\' or an 8.3 short name's '~' + digit anywhere in it, or a trailing dot or space, however long it is.
const windowsAlias = (path: string): boolean => path.split("/").some((comp) => /[:\\]|~[0-9]/.test(comp) || comp.endsWith(".") || comp.endsWith(" "));

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

  // What a name folds to where that can be a protected name: its folded letters while they are ASCII and no more than
  // the rule compares, else null. The rule lower-cases ASCII alone, and protect.ts all of a name: no other character's
  // lower case is ASCII, so either folds it to no ASCII name.
  const SG_NAMELEN = 16; // rule-match.h: the bytes of a folded name it compares
  const ascii = (name: string) => /^[\x00-\x7f]*$/.test(name);
  const folds = (names: string[]) => execFileSync(harness(), ["fold"], { input: `${names.join("\n")}\n`, encoding: "utf8", maxBuffer: 1 << 26 })
    .split("\n").slice(0, -1).map((line) => line.split("\t"));
  const matchable = (names: string[]) => folds(names)
    .map(([length = "", bytes = ""], i) => [names[i]!, Number(length) <= SG_NAMELEN && ascii(bytes) ? bytes : null]);
  const matchableOf = (name: string) => {
    const fold = folded(name);
    return [name, ascii(fold) && fold.length <= SG_NAMELEN ? fold : null];
  };

  it("folds each name as protect.ts folds it", () => {
    const names = [
      "ſ", "ı", "İ", "ß", "ẞ", "\u212A", "ﬀ", "ﬁ", "ﬂ", "ﬃ", "ﬄ", "ﬅ", "ﬆ", "HOOKS", ".GİT", "conﬁg.wor\u212Atree", "ẞſßı", "Straße",
      "x".repeat(SG_NAMELEN), "ı".repeat(15), "\u212A".repeat(10), "x".repeat(31), "x".repeat(40), "é", "ÉCOLE",
      "hooks.", ".git. .", "ho\u200coks", "\ufeff", ".", " . ", ".x.", `${"x".repeat(40)}.`, `.git${".".repeat(300)}`,
      `${"\u200c".repeat(200)}hooks`, "HOOK\u206aS\u206f \u200e.", `x${" ".repeat(20)}y `, "\u200b", "\u2010.", "\ufefe ",
    ];
    // An ASCII name folded, its length and the bytes compared, read whole however long it is.
    const want = (name: string) => {
      const fold = folded(name);
      return ascii(fold) ? [name, `${fold.length}\t${fold.slice(0, SG_NAMELEN)}`] : [name, "not ASCII"];
    };
    expect(folds(names).map(([length = "", bytes = ""], i) => [names[i]!, ascii(bytes) ? `${length}\t${bytes}` : "not ASCII"]))
      .toEqual(names.map(want));
  });

  // Names made at random of what either layer drops, folds or keeps, and protected names written with those, folded by
  // both; then each in the places a protected name counts, judged by both.
  it("folds and judges names made at random of what either drops, folds or keeps, as protect.ts does", () => {
    let seed = 0x5eed;
    const random = (n: number) => {
      seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
      return (seed >>> 8) % n;
    };
    const pick = <T>(of: readonly T[]) => of[random(of.length)]!;
    // What HFS+ ignores (TN1150), and the code points beside it, which it keeps.
    const IGNORED = [..."\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u206a\u206b\u206c\u206d\u206e\u206f\ufeff"];
    const BESIDE = [..."\u200b\u2010\u2029\u202f\u2069\u2070\ufefe\uff00"];
    const LETTERS: Record<string, string[]> = { s: ["ſ", "S"], i: ["ı", "İ", "I"], k: ["\u212a", "K"], ss: ["ß", "ẞ"], fi: ["ﬁ"], fl: ["ﬂ"], st: ["ﬅ", "ﬆ"] };
    const CHARACTERS = [...IGNORED, ...BESIDE, ...Object.values(LETTERS).flat(), ...". aéÉ-_xü\u0307", ..."gitohkscnfvdemjprlwa"];
    const NAMED = [...PROTECTED_NAMES, ...PROTECTED_PAIRS.flat(), ...GIT_CONFIGS, ...GIT_STATE, ...DEPENDENCY_FOLDERS, "modules"];
    // A name with characters put in after its letters, each letter perhaps written as another that folds to it, and an end.
    const spelled = (name: string) => {
      let out = "";
      for (let at = 0; at < name.length;) {
        const pair = LETTERS[name.slice(at, at + 2)];
        const one = LETTERS[name[at]!];
        if (pair && random(3) === 0) {
          out += pick(pair);
          at += 2;
        } else {
          out += one && random(3) === 0 ? pick(one) : random(4) === 0 ? name[at]!.toUpperCase() : name[at]!;
          at += 1;
        }
        while (random(5) === 0) out += pick([...IGNORED, ...BESIDE, ".", " "]);
      }
      while (random(2) === 0) out += pick([...IGNORED, ...BESIDE, ".", " ", ".", " "]);
      return out;
    };
    const names: string[] = [];
    for (let n = 0; n < 20_000; n += 1) {
      names.push(spelled(pick(NAMED)));
      names.push(Array.from({ length: 1 + random(20) }, () => pick(CHARACTERS)).join(""));
    }
    expect(matchable(names)).toEqual(names.map(matchableOf));
    const paths = names.flatMap((name) => [name, `${name}/x`, `.git/${name}/x`, `${name}/hooks/x`, `.claude/${name}/x`, `${name}/p/.vscode/x`]);
    const kernel = verdicts(harness(), paths);
    const bad = paths.flatMap((p, i) => (kernel[i] === ruleRefuses(p) ? [] : [`${JSON.stringify(p)}: rule=${kernel[i]} expected=${ruleRefuses(p)}`]));
    expect(bad).toEqual([]);
  });

  it("refuses each name a volume that compares names without case takes as a protected one, as protect.ts does", () => {
    const paths = [...ALIASES, ...NOT_ALIASES];
    const kernel = verdicts(harness(), paths);
    expect(paths.map((p, i) => [p, kernel[i], onHost(p)]))
      .toEqual(paths.map((p) => [p, ALIASES.includes(p), ALIASES.includes(p)]));
    // A directory moved out of a folded dependency folder carries what was unpacked there too.
    expect(verdicts(harness(), ["node_moduleſ/p\tplanted", "diſt-packages/p\tſite-packages/p"], ["rename"])).toEqual([true, false]);
    expect([movesOutOfDependency("/f", "/f/node_moduleſ/p", "/f/planted"), movesOutOfDependency("/f", "/f/diſt-packages/p", "/f/ſite-packages/p")]).toEqual([true, false]);
    // Each name is read whole, however long: what a volume drops may be most of it.
    expect(verdicts(harness(), [`.git/${"x".repeat(40)}`, `.git/hooKs`, `.git/hooks${"\u200c".repeat(1000)}/x`, `.vscode${".".repeat(2000)}`])).toEqual([false, true, true, true]);
  });

  it("refuses the names Windows's Explorer acts on where the kernel's line names that set, and with posix's decides as it does without one", () => {
    const paths = [...CORPUS, ...probes, ...GIT_STATE_ONLY, ...ALIASES, ...NOT_ALIASES, ...EXPLORER_CORPUS];
    const windows = verdicts(harness(), paths, ["windows"]);
    const bad = paths.flatMap((p, i) => {
      const want = ruleRefuses(p) || explorerActsOn(p) || windowsAlias(p);
      return windows[i] === want ? [] : [`${p}: rule=${windows[i]} expected=${want}`];
    });
    expect(bad).toEqual([]);
    // posix's, as Linux's and macOS's lines name it: the rule's own decisions alone, the Explorer's names among them.
    const posix = verdicts(harness(), EXPLORER_CORPUS);
    expect(EXPLORER_CORPUS.flatMap((p, i) => (posix[i] === ruleRefuses(p) ? [] : [`${p}: rule=${posix[i]} expected=${ruleRefuses(p)}`]))).toEqual([]);
    // The corpus holds both answers, about both ends of the letters the rule keeps of a name's end.
    expect(EXPLORER_CORPUS.filter(explorerActsOn).length).toBeGreaterThan(250);
    expect(EXPLORER_CORPUS.filter((p) => !explorerActsOn(p) && !ruleRefuses(p)).length).toBeGreaterThan(300);
    // Each name read whole, however long: what a volume drops may be most of it.
    expect(verdicts(harness(), [
      `${"a".repeat(200)}.searchconnector-ms`, `${"€".repeat(9)}.ſearchconnector-mſ`, "desktop.ini", `${"a".repeat(13)}desktop.ini`,
      `desktop.ini${"\u200c".repeat(1000)}`, `${"\ufeff".repeat(500)}autorun.inf`, `x.ln${"\u206f".repeat(800)}k`, `${"a".repeat(13)}desktop.ini${"\u200c".repeat(100)}`,
    ], ["windows"])).toEqual([true, true, true, false, true, true, true, false]);
  });

  it("refuses, under windows, a component NTFS may resolve to another name, and leaves posix deciding each as it does", () => {
    // The reviewer's six, with a trailing-space form and a second 8.3 short name: each a path whose protected or
    // Explorer target NTFS may resolve it to, which windows refuses for its form.
    const aliases = [
      "GIT~1/hooks/pre-commit", ".git./hooks/x", ".git /hooks/x", ".git::$INDEX_ALLOCATION/hooks/x",
      "x.lnk::$DATA", "desktop.ini.", "desktop.ini ", "sub\\desktop.ini", "DOCUME~1.LIB", "notes~9.txt",
    ];
    expect(verdicts(harness(), aliases, ["windows"])).toEqual(aliases.map(() => true));
    expect(aliases.every(windowsAlias)).toBe(true);
    // posix judges none of these forms as such: it decides each by the name a volume takes it as, which refuses the
    // two that one dropping a name's trailing dots and spaces takes as .git, and no other.
    expect(verdicts(harness(), aliases)).toEqual(aliases.map((p) => ruleRefuses(p)));
    expect(aliases.filter(ruleRefuses)).toEqual([".git./hooks/x", ".git /hooks/x"]);
    // Their near neighbours, which hold no such form: windows refuses only what the rule or an Explorer name already does.
    const near = ["GIT1/hooks/x", "git~x/hooks/x", "a.lnkx", "notes.txt", "sub.desktop.ini", "~/x", "a~/b", `${"x".repeat(300)}~`, `${"y".repeat(300)}.txt`];
    expect(near.some(windowsAlias)).toBe(false);
    expect(verdicts(harness(), near, ["windows"])).toEqual(near.map((p) => ruleRefuses(p) || explorerActsOn(p)));
    // Each read whole, however long: a form deep in a long name, or at its end, is refused all the same.
    const long = [`${"x".repeat(40)}.`, `${"x".repeat(300)} `, `${"x".repeat(200)}~1`, `${"x".repeat(1000)}:s/x`, `${"é".repeat(150)}\\desktop.ini`, `${"\u200c".repeat(100)}.git.`];
    expect(verdicts(harness(), long, ["windows"])).toEqual(long.map(() => true));
    expect(long.every((p) => windowsAlias(p))).toBe(true);
  });

  // The rule folds a name's bytes, and leaves each byte of a character it does not fold as it is: protect.ts's fold of
  // these, as UTF-8 writes it, is the rule's.
  it("folds a name's end as protect.ts folds the name, read whole: its length, and its last 24 letters", () => {
    const names = [
      "Desktop.INI", "ﬁle.LNK", "deſ\u212Atop.ını", "ẞtraße.url", "x", `${"a".repeat(40)}.Library-MS`, `${"€".repeat(9)}.ſearchconnector-mſ`,
      `desktop.ini${"\u200c".repeat(40)}`, "x.lnk. . .", `\ufeff.url\u200f`, `${"a".repeat(3000)}.L\u200dNK${". ".repeat(500)}`, "\u200c. .", "",
    ];
    const out = execFileSync(harness(), ["end"], { input: `${names.join("\n")}\n` }).toString("latin1").split("\n").slice(0, -1);
    expect(out).toEqual(names.map((name) => {
      const bytes = Buffer.from(folded(name), "utf8");
      return `${bytes.length}\t${bytes.subarray(Math.max(0, bytes.length - 24)).toString("latin1")}`;
    }));
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
