// On Windows nothing contains the file helper but its walk (src/files/fd-paths-win32.ts): Node's own fs calls on a path
// follow every reparse point there. So no file kind reaches a folder's file by Node's own call on its path: every one
// goes through fdPaths. A landing's (land.ts) does not run on Windows yet: its helper refuses to start there.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

const FILES = fileURLToPath(new URL("../src/files", import.meta.url));
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// Node's calls that take a path, each of which would open a folder's file by it.
const BY_PATH = new Set([
  "access", "appendFile", "chmod", "chown", "copyFile", "cp", "exists", "lchmod", "lchown", "link", "lstat", "lutimes", "mkdir", "mkdtemp", "open",
  "opendir", "readdir", "readFile", "readlink", "realpath", "rename", "rm", "rmdir", "stat", "statfs", "symlink", "truncate", "unlink", "utimes",
  "watch", "writeFile",
]);

// The calls by path a module's text makes of what it imports from Node's fs, each by its name without Sync.
function byPath(text: string): string[] {
  const imported = [...text.matchAll(/import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*"node:fs(?:\/promises)?"/g)].flatMap(([, names]) =>
    names!.split(",").map((name) => name.trim()).filter((name) => name && !name.startsWith("type ")).map((name) => name.split(/\s+as\s+/).at(-1)!));
  const calls = imported.filter((name) => BY_PATH.has(name.replace(/Sync$/, "")));
  if (calls.length === 0) return [];
  const call = new RegExp(`(?<![.\\w])(${calls.join("|")})\\s*\\(`, "g");
  return [...text.matchAll(call)].map((match) => match[1]!.replace(/Sync$/, ""));
}

// The file kinds' modules on Windows, and the calls by path each may make: operations.ts looks for the helper's rg on its
// PATH (findOnPath, the app's own programs); paths.ts reads a descriptor fdPaths opened; os.ts finds rg.exe on the PATH.
const KINDS: Record<string, string[]> = {
  "operations.ts": ["stat", "access"],
  "paths.ts": ["readFile"],
  "protect.ts": [],
  "edge.ts": [],
  "answers.ts": [],
  "helper.ts": [],
  "os.ts": ["stat"],
};

it("reach no folder's file on Windows by Node's own call on its path, only through the walk", () => {
  const found = Object.fromEntries(Object.keys(KINDS).map((file) => {
    return [file, byPath(code(readFileSync(join(FILES, file), "utf8")))];
  }));
  expect(found).toEqual(KINDS);
  // operations.ts's are findOnPath's, in runnable alone.
  const operations = code(readFileSync(join(FILES, "operations.ts"), "utf8"));
  const runnable = /function runnable\([^)]*\)[^{]*\{[\s\S]*?\n\}/.exec(operations)![0];
  expect(byPath(`import { accessSync, statSync } from "node:fs";\n${runnable}`)).toEqual(["stat", "access"]);
  // paths.ts's reads what fdPaths opened.
  expect(code(readFileSync(join(FILES, "paths.ts"), "utf8"))).toMatch(/fdPaths\.open\("\/etc\/passwd"[^)]*\);\s*try \{\s*passwd = readFileSync\(fd, "utf8"\)/);
});
