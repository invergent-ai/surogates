// The file host's calls that are not the same on every OS, each in its own module: an open flag spelled by its
// number in files/flags.ts alone, since a flag's number on one OS can be another flag on the next (Linux's O_PATH is
// Darwin's O_SYMLINK), and a path through a descriptor's /proc entry in Linux's FdPaths alone.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { LINUX_FLAGS } from "../src/files/flags.js";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
// What the file host runs: the helper's operations and the host around it.
const SCANNED = ["files", "hosts"];
const FLAGS_MODULE = "files/flags.ts";
// Each OS's FdPaths, which holds its folders by its own flags.
const OWN_PATHS = /^files\/fd-paths-[a-z]+\.ts$/;
const LINUX_PATHS = "files/fd-paths-linux.ts";

const LITERAL = String.raw`(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|\d[\d_]*)`;
const FLAG = String.raw`(?:\bconstants\.O_\w+|\bFLAGS\.\w+|\bO_[A-Z_]+)`;
const RULES: Array<[string, RegExp]> = [
  ["O_PATH named", /\bO_PATH\b/g],
  // A mode's bits all fit in 0o7777: an octal number past them is a flag's.
  ["an octal flag", /\b0[oO]([0-7_]+)\b/g],
  ["a number beside a flag", new RegExp(String.raw`${FLAG}\s*\|\s*${LITERAL}\b|\b${LITERAL}\s*\|\s*${FLAG}`, "g")],
  ["a number as an open's flags", new RegExp(String.raw`\b(?:openSync|open|openat|hold)\(\s*[^,()]+,\s*${LITERAL}\s*[,)]`, "g")],
];

// The code without its comments: a comment may name a flag, and nothing runs it.
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// Each open flag *text* spells by its number or as O_PATH, as "<rule>: <what>".
function spelledFlags(text: string): string[] {
  const found: string[] = [];
  for (const [rule, pattern] of RULES) {
    for (const match of code(text).matchAll(pattern)) {
      if (rule === "an octal flag" && Number.parseInt(match[1]!.replaceAll("_", ""), 8) <= 0o7777) continue;
      found.push(`${rule}: ${match[0]}`);
    }
  }
  return found;
}

const sources = (dir: string): string[] =>
  readdirSync(join(SRC, dir), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sources(join(dir, entry.name)) : entry.name.endsWith(".ts") ? [join(dir, entry.name)] : []);

describe("the file host's open flags", () => {
  it("are found wherever they are spelled by their number, and a mode, a comment or Node's own name is none", () => {
    expect(spelledFlags("const HOLD = 0o10000000 | constants.O_DIRECTORY | constants.O_NOFOLLOW;")).toEqual([
      "an octal flag: 0o10000000", "a number beside a flag: 0o10000000 | constants.O_DIRECTORY",
    ]);
    expect(spelledFlags("const O_PATH = 2097152;")).toEqual(["O_PATH named: O_PATH"]);
    expect(spelledFlags("openSync(path, 0x200000)")).toEqual(["a number as an open's flags: openSync(path, 0x200000)"]);
    expect(spelledFlags("open(path, FLAGS.DIRECTORY | 65536)")).toEqual(["a number beside a flag: FLAGS.DIRECTORY | 65536"]);
    expect(spelledFlags("const CLOEXEC = 0o2000000;")).toEqual(["an octal flag: 0o2000000"]);
    expect(spelledFlags([
      "// O_PATH, which Node does not name: 0o10000000",
      "/* openSync(path, 0x200000) */",
      "chmodSync(path, 0o7777 & mode); openSync(temp, constants.O_WRONLY | constants.O_CREAT, 0o600); openSync(at, \"w\", 0o600);",
    ].join("\n"))).toEqual([]);
  });

  it("are spelled by their number only in their own module, and named only there and in each OS's FdPaths", () => {
    const spelled = SCANNED.flatMap(sources).filter((file) => file !== FLAGS_MODULE && !OWN_PATHS.test(file))
      .flatMap((file) => spelledFlags(readFileSync(join(SRC, file), "utf8")).map((one) => `${file}: ${one}`));
    expect(spelled).toEqual([]);
  });

  it("are Linux's own numbers for the two Node does not name", () => {
    expect(LINUX_FLAGS).toEqual({ O_PATH: 0o10000000, O_CLOEXEC: 0o2000000 });
  });
});

describe("the file host's paths through a descriptor", () => {
  it("are Linux's FdPaths' alone", () => {
    const through = SCANNED.flatMap(sources).filter((file) => file !== LINUX_PATHS)
      .filter((file) => /\/proc\/self\/fd/.test(code(readFileSync(join(SRC, file), "utf8"))));
    expect(through).toEqual([]);
  });
});
