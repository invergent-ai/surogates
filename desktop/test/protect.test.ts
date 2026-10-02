import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { checkWrite, inFolderRefusal, protectedInFolder } from "../src/files/protect.js";

const home = "/home/tester";
let folder: string;

beforeEach(() => {
  folder = realpathSync(mkdtempSync(join(tmpdir(), "protect-")));
});

afterEach(() => {
  rmSync(folder, { recursive: true, force: true });
});

describe("checkWrite, with the cloud's two lists", () => {
  it.each(["/etc/passwd", "/etc/shadow", "~/.ssh/id_rsa", "~/.ssh/new_key", "~/.aws/credentials", "~/.config/gh/hosts.yml", "~/.bashrc", "/etc/systemd/x"])(
    "denies %s as a protected system or credential file",
    (path) => {
      expect(checkWrite(folder, home, path)).toBe(`Write denied: '${path}' is a protected system/credential file.`);
    },
  );

  it.each(["/etc/hosts", "/boot/grub.cfg", "/usr/lib/systemd/x", "/run/docker.sock"])(
    "refuses the sensitive system path %s",
    (path) => {
      expect(checkWrite(folder, home, path)).toBe(
        `Refusing to write to sensitive system path: ${path}\nUse the terminal tool with sudo if you need to modify system files.`,
      );
    },
  );

  it("matches the credential folders' contents, not the folders themselves", () => {
    expect(checkWrite(folder, home, "~/.ssh")).toBeNull();
  });

  it("refuses a NUL byte as Python does", () => {
    expect(() => checkWrite(folder, home, "a\0b")).toThrow("embedded null byte");
  });
});

describe("checkWrite, in the folder", () => {
  it.each([".git/config", ".git/hooks/pre-commit", "sub/.GIT/Hooks/x", ".vscode/settings.json", "a/b/c/d/.bashrc", ".mcp.json", ".claude/commands/x.md", ".Idea/workspace.xml", ".git", "sub/.git"])(
    "refuses %s, which could run code outside the sandbox",
    (path) => {
      expect(checkWrite(folder, home, path)).toBe(inFolderRefusal(path));
      expect(inFolderRefusal(path)).toBe(
        `Write denied: '${path}' is protected in this folder: a change to it could run code outside the sandbox.`,
      );
    },
  );

  it.each(["a.txt", ".gitignore", ".git/HEAD", ".env", ".claude/settings.json", "sub/bashrc"])("allows %s", (path) => {
    expect(checkWrite(folder, home, path)).toBeNull();
  });
});

describe("protectedInFolder", () => {
  it("judges a key in the folder, never the folder itself", () => {
    expect(protectedInFolder(folder, `${folder}/.git/config`)).toBe(true);
    expect(protectedInFolder(folder, `${folder}/src/.vscode`)).toBe(true);
    expect(protectedInFolder(folder, `${folder}/a.txt`)).toBe(false);
    expect(protectedInFolder(folder, folder)).toBe(false);
  });
});
