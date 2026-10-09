import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Failure, NUL_REFUSED } from "../src/files/answers.js";
import { checkWrite, inFolderRefusal, movesOutOfDependency, protectedInFolder } from "../src/files/protect.js";

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

  it.each(["a.txt", ".gitignore", ".git/HEAD", ".env", ".claude/settings.json", "sub/bashrc"])("allows %s", (path) => {
    expect(checkWrite(folder, home, path)).toBeNull();
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

  // Package managers unpack what packages ship, .idea and .vscode folders among it (iconv-lite's).
  it.each([
    "node_modules/iconv-lite/.idea/codeStyles/Project.xml", "a/node_modules/b/.vscode/settings.json", "NODE_MODULES/b/.mcp.json",
    ".venv/lib/python3.12/site-packages/pkg/.vscode/settings.json", "usr/lib/python3/dist-packages/pkg/.bashrc",
    "node_modules/pkg/.claude/commands/x.md", "node_modules/pkg/.claude/agents/y.md",
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
  ])("judges a directory moved from %s to %s (exchange: %s): out of a dependency folder %s", (from, to, exchange, out) => {
    expect(movesOutOfDependency(folder, `${folder}/${from}`, `${folder}/${to}`, exchange)).toBe(out);
  });
});
