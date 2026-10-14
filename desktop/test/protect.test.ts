import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Failure, NUL_REFUSED } from "../src/files/answers.js";
import { checkWrite, folded, inFolderRefusal, movesOutOfDependency, protectedInFolder } from "../src/files/protect.js";

const home = "/home/tester";
let folder: string;

beforeEach(() => {
  folder = realpathSync(mkdtempSync(join(tmpdir(), "protect-")));
});

afterEach(() => {
  rmSync(folder, { recursive: true, force: true });
});

describe("checkWrite, with the cloud's two lists", () => {
  describe("HOME_FILES", () => {
    it.each([
      "~/.ssh/authorized_keys", "~/.ssh/id_rsa", "~/.ssh/id_ed25519", "~/.ssh/config",
      "~/.bashrc", "~/.zshrc", "~/.profile", "~/.bash_profile", "~/.zprofile",
      "~/.netrc", "~/.pgpass", "~/.npmrc", "~/.pypirc",
    ])("denies %s as a protected credential file", (path) => {
      expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
    });
  });

  describe("SYSTEM_FILES", () => {
    it.each(["/etc/sudoers", "/etc/passwd", "/etc/shadow"])("denies %s as a protected system file", (path) => {
      expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
    });
  });

  describe("HOME_FOLDERS contents", () => {
    it.each([
      "~/.ssh/x", "~/.aws/x", "~/.gnupg/x", "~/.kube/x", "~/.docker/x", "~/.azure/x", "~/.config/gh/x",
    ])("denies %s inside a protected credential folder", (path) => {
      expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
    });
  });

  describe("SYSTEM_FOLDERS contents", () => {
    it.each(["/etc/sudoers.d/x", "/etc/systemd/x"])("denies %s inside a protected system folder", (path) => {
      expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
    });
  });

  describe("SYSTEM_FOLDERS themselves", () => {
    it("refuses /etc/sudoers.d with the sensitive message", () => {
      expect(checkWrite(folder, home, "/etc/sudoers.d")).toBe(
        `Refusing to write to sensitive system path: /etc/sudoers.d\nUse the terminal tool with sudo if you need to modify system files.`,
      );
    });
  });

  describe("SENSITIVE_PREFIXES and SENSITIVE_PATHS", () => {
    it.each(["/etc/hosts", "/etc/shadow.bak", "/boot/grub.cfg", "/usr/lib/systemd/x", "/var/run/docker.sock", "/run/docker.sock"])(
      "refuses the sensitive system path %s",
      (path) => {
        expect(checkWrite(folder, home, path)).toBe(
          `Refusing to write to sensitive system path: ${path}\nUse the terminal tool with sudo if you need to modify system files.`,
        );
      },
    );
  });

  it("matches the credential folders' contents, not the folders themselves", () => {
    expect(checkWrite(folder, home, "~/.ssh")).toBeNull();
  });

  it("refuses a NUL in the cloud's sentence, with a Failure refusal of type 'value'", () => {
    try {
      checkWrite(folder, home, "a\0b");
      throw new Error("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(Failure);
      expect((error as Failure).refusal).toEqual({ type: "value", message: NUL_REFUSED });
    }
  });
});

describe("checkWrite, with relative paths escaping the folder", () => {
  it("denies ../../../../../../etc/passwd with the protected system file message", () => {
    const path = "../../../../../../etc/passwd";
    expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
  });

  it("refuses ../../../../../../etc/hosts with the sensitive path message", () => {
    const path = "../../../../../../etc/hosts";
    expect(checkWrite(folder, home, path)).toBe(
      `Refusing to write to sensitive system path: ${path}\nUse the terminal tool with sudo if you need to modify system files.`,
    );
  });
});

describe("checkWrite, in the folder", () => {
  describe("PROTECTED_NAMES", () => {
    it.each([
      ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
      ".ripgreprc", ".mcp.json", ".vscode/x", ".idea/x",
    ])("refuses %s, which could run code outside the sandbox", (path) => {
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
    });
  });

  describe("PROTECTED_PAIRS", () => {
    it.each([
      ".claude/commands/x", ".claude/agents/x", ".git/hooks/x", ".git/config",
    ])("refuses %s, which could run code outside the sandbox", (path) => {
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
    });
  });

  describe(".git file itself", () => {
    it("refuses .git as a protected file", () => {
      expect(checkWrite(folder, home, ".git")).toBe(inFolderRefusal(".git"));
    });

    it("refuses sub/.git as a protected file", () => {
      const path = "sub/.git";
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
    });
  });

  describe("case-insensitive matching at any depth", () => {
    it.each([
      "sub/.GIT/Hooks/x", "a/b/c/d/.BASHRC", ".VSCODE/settings.json", ".Idea/workspace.xml",
    ])("refuses %s regardless of case", (path) => {
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
    });
  });

  // HFS+ compares names without the code points it ignores (Apple's TN1150), and FAT's and exFAT's volumes drop the
  // dots and spaces a name ends with: on such a volume each of these is a protected name.
  describe("as a volume that drops what a name holds takes it", () => {
    it.each([
      ".git./hooks/x", ".git/ho‌oks/x", ".vscode /settings.json", ".mcp.json.", "﻿.idea/x", ".git/config. .",
      ".claude/commands‍/x", ".git‪/config", "sub/.git.", ".bashrc⁯", ".Git ‎./HOOKS./x", ".gıt./conﬁg",
      `.git/hooks${".".repeat(300)}/x`, `.git/ho${"‌".repeat(300)}oks/x`,
    ])("refuses %s", (path) => {
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
    });

    // U+200B, U+2010, U+2029, U+202F, U+2069, U+2070 and U+FEFE lie beside what HFS+ ignores, and it keeps them; a dot
    // or a space a name holds before its end is kept too.
    it.each([
      "notes.", "a b /c", ".gi.t/hooks/x", ".git/ho​oks/x", ".git/‐hooks/x", ".git/hooks /x", ".git/ hooks/x",
      ".git/hooks⁩/x", ".git/⁰hooks/x", "﻾.vscode/x", ".git/refs/heads/x.", "src‌/main.c", ".vscodex.", ". .vscode/x",
    ])("allows %s", (path) => {
      expect(checkWrite(folder, home, path)).toBeNull();
    });
  });

  it.each(["a.txt", ".gitignore", ".git/HEAD", ".env", ".claude/settings.json", "sub/bashrc"])("allows %s", (path) => {
    expect(checkWrite(folder, home, path)).toBeNull();
  });
});

// APFS compares names as Unicode's canonical equivalence takes them, then without case: a name and its decomposed
// spelling are one name there, as each of these pairs is.
describe("the fold, as APFS compares names", () => {
  it.each([
    ["caf\u00e9", "cafe\u0301"], ["CAF\u00c9", "cafe\u0301"], ["CAFE\u0301", "caf\u00e9"], ["\uac00", "\u1100\u1161"],
    // Two marks in either order, which canonical ordering puts in one.
    ["a\u0301\u0328", "a\u0328\u0301"], ["\u01fa", "A\u030a\u0301"], ["\u212a", "k"],
  ])("takes %s and %s as one", (one, other) => {
    expect(folded(one)).toBe(folded(other));
  });

  it("takes as a protected name each spelling it took before, the Kelvin sign's by its canonical K, and none with a mark on a letter", () => {
    for (const path of [".git/hoo\u212As/x", ".git/hooKs/x", ".g\u0130t/config", ".g\u0131t/config", ".vſcode/x", ".git/conﬁg"]) {
      expect(checkWrite(folder, home, path), path).toBe(inFolderRefusal(path));
    }
    // A mark on a letter of one is another name, to APFS and to the fold, in either spelling: no write lands in .git. İ
    // decomposed, I and a dot above, is a letter with a mark too.
    for (const path of [".g\u00edt/config", ".gi\u0301t/config", ".vsc\u00f6de/x", ".vsco\u0308de/x", ".mcp.jso\u0303n", ".gI\u0307t/config"]) {
      expect(checkWrite(folder, home, path), path).toBeNull();
    }
  });
});

// What else in a .git folder sends git to a config or hooks of the agent's choosing.
const GIT_RUNS_CODE = [
  ".git/commondir", ".git/config.worktree", ".git/config",
  ".git/modules/sub/config", ".git/modules/sub/hooks/pre-commit", ".git/modules/sub/hooks",
  ".git/worktrees/w/config.worktree", ".git/worktrees/w/commondir", ".git/worktrees/w/anything", ".git/worktrees",
  ".git/modules/a/modules/b/config", ".git/hooks/pre-commit", ".git/modules/a/hooks/x", ".git/worktrees/w/config",
  // A submodule's name can hold slashes, and its git folder has worktrees and paused state of its own.
  ".git/modules/libs/foo/config", ".git/modules/libs/foo/hooks/pre-commit", ".git/modules/sub/worktrees/w/config",
  ".git/modules/sub/worktrees/w/commondir", ".git/modules/sub/rebase-merge/git-rebase-todo",
  // A paused rebase or cherry-pick runs the exec lines of its todo on --continue.
  ".git/rebase-merge/git-rebase-todo", ".git/rebase-apply/next", ".git/sequencer/todo",
];
const GIT_RUNS_CODE_ANYWHERE = [
  ...GIT_RUNS_CODE,
  ...GIT_RUNS_CODE.map((path) => `sub/deeper/${path}`),
  ...GIT_RUNS_CODE.map((path) => path.replace(/[a-z]+/g, (word) => word.charAt(0).toUpperCase() + word.slice(1))),
  ...GIT_RUNS_CODE.map((path) => `Sub/${path.toUpperCase()}`),
];
const GIT_STAYS_OPEN = [
  ".git/HEAD", ".git/info/exclude", ".git/objects/ab/cdef", ".git/refs/heads/main", ".git/index", ".git/modules/sub/HEAD",
  ".git/modules/sub/objects/ab", ".git/modules/sub/info/exclude", "sub/.git/HEAD",
  "commondir", "config", "config.worktree", "hooks/x", "worktrees/w/config", "sub/commondir", "gitconfig/x",
  // Branch and tag names are the user's: a ref named hooks or config runs nothing.
  ".git/refs/heads/fix/hooks", ".git/logs/refs/heads/fix/hooks", ".git/refs/heads/chore/config", ".git/refs/tags/config",
];

describe("the rest of a .git folder that runs code", () => {
  it.each(GIT_RUNS_CODE_ANYWHERE)("checkWrite refuses %s", (path) => {
    expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
  });

  it.each(GIT_RUNS_CODE_ANYWHERE)("protectedInFolder protects %s", (path) => {
    expect(protectedInFolder(folder, `${folder}/${path}`)).toBe(true);
  });

  it.each(GIT_STAYS_OPEN)("leaves %s open to writes", (path) => {
    expect(checkWrite(folder, home, path)).toBeNull();
    expect(protectedInFolder(folder, `${folder}/${path}`)).toBe(false);
  });
});

describe("protectedInFolder", () => {
  it("judges a key in the folder, never the folder itself", () => {
    expect(protectedInFolder(folder, `${folder}/.git/config`)).toBe(true);
    expect(protectedInFolder(folder, `${folder}/src/.vscode`)).toBe(true);
    expect(protectedInFolder(folder, `${folder}/a.txt`)).toBe(false);
    expect(protectedInFolder(folder, folder)).toBe(false);
  });

  it("judges a key in time that grows with its length, however many parts it has and whatever they are", () => {
    // The longest path Linux takes, made of names of a letter each; of git folders, a submodule's, a dependency folder's.
    for (const part of ["a", ".git", ".git/modules/a/b", "node_modules", ".claude", "modules/hooks"]) {
      const deep = `${part}/`.repeat(Math.floor(4_090 / (part.length + 1)));
      const began = performance.now();
      for (let n = 0; n < 2_000; n += 1) protectedInFolder(folder, `${folder}/${deep}x`);
      // Loosely, for a loaded computer: a fifth of a second where this was written, and half a minute with every part before each looked at again.
      expect(performance.now() - began, part).toBeLessThan(2_000);
    }
  });

  // Package managers unpack what packages ship, .idea and .vscode folders among it (iconv-lite's).
  it.each([
    "node_modules/iconv-lite/.idea/codeStyles/Project.xml", "a/node_modules/b/.vscode/settings.json", "NODE_MODULES/b/.mcp.json",
    ".venv/lib/python3.12/site-packages/pkg/.vscode/settings.json", "usr/lib/python3/dist-packages/pkg/.bashrc",
    "node_modules/pkg/.claude/commands/x.md", "node_modules/pkg/.claude/agents/y.md", "node_modules./pkg/.vscode/settings.json",
  ])("leaves a shell's, editor's or agent's name below a dependency folder open: %s", (path) => {
    expect(protectedInFolder(folder, `${folder}/${path}`)).toBe(false);
  });

  it.each([
    "node_modules/pkg/.gitmodules", "site-packages/pkg/.gitconfig", "node_modules/pkg/.git", "node_modules/pkg/.git/hooks/pre-commit",
    "node_modules/pkg/.git/config", ".vscode/node_modules/x", ".claude/commands/node_modules/x", "node_modules_old/.idea",
  ])("still protects git's names below a dependency folder, and any name above one: %s", (path) => {
    expect(protectedInFolder(folder, `${folder}/${path}`)).toBe(true);
  });

  // What a dependency folder holds goes unjudged: a directory moved out of one would carry it.
  it.each([
    ["node_modules/p", "planted", false, true], ["node_modules", "plain", false, true], ["a/site-packages/r", "a/r", false, true],
    ["x/dist-packages", "x/y", false, true], ["Node_Modules/p", "p", false, true], ["plain", "node_modules/p", true, true],
    ["node_modules/p", "node_modules/.p-retired", false, false], ["plain", "node_modules/p", false, false], ["a", "b", true, false],
    ["node_modules/a/node_modules/b", "node_modules/b", false, false], ["site-packages/r", "dist-packages/r", true, false],
    // As a volume that drops a name's trailing dots and the code points HFS+ ignores takes it, a dependency folder.
    ["node_modules./p", "planted", false, true], ["node_‌modules/p", "planted", false, true], ["plain", "site-packages /p", true, true],
  ])("judges a directory moved from %s to %s (exchange: %s): out of a dependency folder %s", (from, to, exchange, out) => {
    expect(movesOutOfDependency(folder, `${folder}/${from}`, `${folder}/${to}`, exchange)).toBe(out);
  });
});
