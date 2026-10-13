// The file host's calls that are not the same on every OS, each in its own module: an open flag spelled by its
// number in files/flags.ts alone, anywhere in the app, since a flag's number on one OS can be another flag on the next
// (Linux's O_PATH is Darwin's O_SYMLINK); and a path through a descriptor's /proc entry in Linux's FdPaths alone.

import { constants, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { FLAGS, LINUX_FLAGS } from "../src/files/flags.js";
import { importedOn } from "./platforms.js";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
// What the file host runs: the helper's operations and the host around it.
const FILE_HOST = ["files", "hosts"];
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
  // Its first argument may hold a call or an object of its own.
  ["a number as an open's flags", new RegExp(String.raw`\b(?:openSync|open|openat|hold)\(\s*(?:[^,(){}]|\([^()]*\)|\{[^{}]*\})+,\s*${LITERAL}\s*[,)]`, "g")],
];
// Linux's open flags whose numbers are another flag's, or none, on Darwin: found by their value, however a number is
// spelled, or as one bit shifted.
const LINUX_ONLY = new Map([[0x200000, "O_PATH"], [0x80000, "O_CLOEXEC"], [0x10000, "O_DIRECTORY"], [0x20000, "O_NOFOLLOW"], [0x410000, "O_TMPFILE"]]);
const NUMBER = String.raw`(?:0[xX][\da-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)n?`;
const VALUED = new RegExp(String.raw`(?<![\w$.])(${NUMBER})(?:\s*<<\s*(${NUMBER}))?(?![\w$.])`, "g");
const valueOf = (spelled: string) => Number(spelled.replaceAll("_", "").replace(/n$/, ""));
// A number compared with < or > is a bound, a code point's or a port's, which no flag is.
const compared = (before: string, after: string) => /(?:^|[^<>=])[<>]=?\s*$/.test(before) || /^\s*[<>](?:=|[^<>=]|$)/.test(after);

// The code without its comments: a comment may name a flag, and nothing runs it.
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// Each open flag *text* spells by its number or as O_PATH, as "<rule>: <what>".
function spelledFlags(text: string): string[] {
  const found: string[] = [];
  const read = code(text);
  for (const [rule, pattern] of RULES) {
    for (const match of read.matchAll(pattern)) {
      if (rule === "an octal flag" && Number.parseInt(match[1]!.replaceAll("_", ""), 8) <= 0o7777) continue;
      found.push(`${rule}: ${match[0]}`);
    }
  }
  for (const match of read.matchAll(VALUED)) {
    const flag = LINUX_ONLY.get(match[2] === undefined ? valueOf(match[1]!) : valueOf(match[1]!) << valueOf(match[2]));
    const at = match.index;
    if (flag && !compared(read.slice(Math.max(0, at - 16), at), read.slice(at + match[0].length, at + match[0].length + 16))) {
      found.push(`Linux's ${flag}: ${match[0]}`);
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
      "an octal flag: 0o10000000", "a number beside a flag: 0o10000000 | constants.O_DIRECTORY", "Linux's O_PATH: 0o10000000",
    ]);
    expect(spelledFlags("const O_PATH = 2097152;")).toEqual(["O_PATH named: O_PATH", "Linux's O_PATH: 2097152"]);
    expect(spelledFlags("openSync(path, 0x101000)")).toEqual(["a number as an open's flags: openSync(path, 0x101000)"]);
    expect(spelledFlags("open(path, FLAGS.DIRECTORY | 0x101000)")).toEqual(["a number beside a flag: FLAGS.DIRECTORY | 0x101000"]);
    expect(spelledFlags("const SYNC = 0o4010000;")).toEqual(["an octal flag: 0o4010000"]);
    expect(spelledFlags([
      "// O_PATH, which Node does not name: 0o10000000",
      "/* openSync(path, 0x200000) */",
      "chmodSync(path, 0o7777 & mode); openSync(temp, constants.O_WRONLY | constants.O_CREAT, 0o600); openSync(at, \"w\", 0o600);",
    ].join("\n"))).toEqual([]);
  });

  it("are found by their value, in any base or form, where nothing else shows them as flags, and a number compared as a bound is none", () => {
    for (const [line, spelled] of [
      ["const HOLD = 0x200000;", "0x200000"], ["const HOLD = 2097152;", "2097152"], ["const PATH_ONLY = 0x200000 | DIR;", "0x200000"],
      ["const flags = 0x200000; openSync(path, flags)", "0x200000"], ["const HOLD = 0X20_0000;", "0X20_0000"],
      ["const HOLD = 0b1000000000000000000000;", "0b1000000000000000000000"], ["const HOLD = 2_097_152;", "2_097_152"],
      ["const HOLD = 2.097152e6;", "2.097152e6"], ["const HOLD = 0x200000n;", "0x200000n"], ["const HOLD = 1 << 21;", "1 << 21"],
      ["const pick = () => 0x200000;", "0x200000"],
    ]) expect(spelledFlags(line!), line).toEqual([`Linux's O_PATH: ${spelled}`]);
    expect(spelledFlags("const CLOEXEC = 524288, DIRECTORY = 0x10000, NOFOLLOW = 131072, TMPFILE = 0x410000;")).toEqual([
      "Linux's O_CLOEXEC: 524288", "Linux's O_DIRECTORY: 0x10000", "Linux's O_NOFOLLOW: 131072", "Linux's O_TMPFILE: 0x410000",
    ]);
    // An open's flags after a first argument with a call or a comma in it.
    expect(spelledFlags("openSync(join(dir, name), 0x200000)")).toEqual([
      "a number as an open's flags: openSync(join(dir, name), 0x200000)", "Linux's O_PATH: 0x200000",
    ]);
    expect(spelledFlags("fdPaths.open(dir.at(name), 2097152)")).toEqual(["a number as an open's flags: open(dir.at(name), 2097152)", "Linux's O_PATH: 2097152"]);
    expect(spelledFlags("fdPaths.open({ dir: fd, name }, 0x101000)")).toEqual(["a number as an open's flags: open({ dir: fd, name }, 0x101000)"]);
    // A code point's bound and a port's are compared, which no flag is; a product is not read.
    expect(spelledFlags("if (point < 0x10000 || port >= 65_536 || 0x10000 > point) return 64 * 1024;")).toEqual([]);
  });

  it("are spelled by their number only in their own module, anywhere in the app, and named only there and in each OS's FdPaths", () => {
    const spelled = sources("").filter((file) => file !== FLAGS_MODULE).flatMap((file) =>
      spelledFlags(readFileSync(join(SRC, file), "utf8")).filter((one) => !OWN_PATHS.test(file) || !one.startsWith("O_PATH named:"))
        .map((one) => `${file}: ${one}`));
    expect(spelled).toEqual([]);
  });

  it("are Linux's own numbers for the two Node does not name", () => {
    expect(LINUX_FLAGS).toEqual({ O_PATH: 0o10000000, O_CLOEXEC: 0o2000000 });
  });

  it("are each OS's row: no link followed at a path's last name on Linux, and none on an OS the file host is not built for yet", async () => {
    expect(FLAGS.NOFOLLOW).toBe(constants.O_NOFOLLOW);
    const { FLAGS: windows } = await importedOn("win32", () => import("../src/files/flags.js"));
    expect(() => windows.NOFOLLOW).toThrow(new Error("The file host is not built for Windows yet"));
  });
});

describe("the file host's paths through a descriptor", () => {
  it("are Linux's FdPaths' alone", () => {
    const through = FILE_HOST.flatMap(sources).filter((file) => file !== LINUX_PATHS)
      .filter((file) => /\/proc\/self\/fd/.test(code(readFileSync(join(SRC, file), "utf8"))));
    expect(through).toEqual([]);
  });
});
