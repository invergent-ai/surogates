import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import { OUTPUT_CAP_CHARS, pyJsonLength } from "../src/files/answers.js";
import { capStrings, capText, commandOutput, firstPoints, lastPoints, splitLines, stripAnsi, Window } from "../src/hosts/output.js";

const repeat = (bytes: number[] | string, times: number) =>
  Buffer.concat(Array.from({ length: times }, () => (typeof bytes === "string" ? Buffer.from(bytes) : Buffer.from(bytes))));

const CASES: Record<string, [Buffer, Buffer]> = {
  ascii: [Buffer.alloc(700_000, "x"), Buffer.alloc(0)],
  nul: [Buffer.alloc(300_000, 0), Buffer.alloc(0)],
  cjk: [repeat("中", 200_000), Buffer.from("e")],
  astral: [repeat("😀", 300_000), repeat("😀", 10)],
  both: [Buffer.alloc(400_000, "a"), Buffer.alloc(400_000, "b")],
  fits: [Buffer.alloc(262_142, "x"), Buffer.alloc(0)],
  over: [Buffer.alloc(262_143, "x"), Buffer.alloc(0)],
  invalid: [repeat([0xff, 0x41, 0xe2, 0x82], 100_000), Buffer.from([0xc3])],
  stderr: [Buffer.alloc(0), Buffer.alloc(500_000, "y")],
  "short-out": [Buffer.alloc(10, "o"), Buffer.alloc(900_000, "z")],
};

// sha256 of the reference laptop's _cap_text over the same bytes
// (tests/fake_laptop.py at master a845d1e0).
const EXPECTED: Record<string, string> = {
  ascii: "6dcadf9179b1110dab30e128ded8d7e9ed733f845f6d0674bbe5b00b2ccc31ae",
  nul: "beadad307a52f756bec39e097740761d51146c0dd99598b3201b85159e5424eb",
  cjk: "53915f0e10ee53941a8f4c8284192f343f0c6c84dfc3f3d3ff1e6f72a2d53bb0",
  astral: "ec97e339f8779a09904ff47129f60bc96d2f135c04bc94c131da762c633e5d0a",
  both: "449d13ba77a1b06f49ce46cb02de2115661113ea70348902a9c1e3738ffd0f10",
  fits: "ec7f9170afa04085f61efd1d53f7b2b26c3be2bca54695e3a6263d8f4b295ecf",
  over: "4bcca8581ae56cc25be42d8222c14d55b77d11ac28ddfe0ffeb3404638700f95",
  invalid: "d7849ae9884e5e658866e8b08f9543b7b3bf3be92af618d86049bceca3b7d239",
  stderr: "661a54c960a59cbde1e2f5f011f7da66d330884d1853d810bfbb17c6c5259fa9",
  "short-out": "a893a766e308d69ceba9b36c7665b03e4de0bc712afe2c59f8c20a53f12ed085",
};

// A stream read in chunks of every size, as a pipe hands it over.
function windowOf(bytes: Buffer, seed: number): Window {
  const window = new Window();
  let state = seed;
  for (let at = 0; at < bytes.length;) {
    state = (state * 1103515245 + 12345) % 2 ** 31;
    const size = 1 + (state % 65_536);
    window.push(bytes.subarray(at, at + size));
    at += size;
  }
  window.end();
  return window;
}

const sha = (text: string) => createHash("sha256").update(text).digest("hex");

describe("command output", () => {
  it.each(Object.keys(CASES))("is the reference laptop's for %s, whatever the chunking", (name) => {
    const [out, err] = CASES[name] ?? [Buffer.alloc(0), Buffer.alloc(0)];
    for (const seed of [1, 7, 42]) {
      expect(sha(commandOutput(windowOf(out, seed), windowOf(err, seed + 1)))).toBe(EXPECTED[name]);
    }
  });

  it("puts a newline between stdout and stderr only when both have something", () => {
    const of = (text: string) => windowOf(Buffer.from(text), 3);
    expect(commandOutput(of("out\n"), of("err\n"))).toBe("out\n\nerr\n");
    expect(commandOutput(of(""), of("err"))).toBe("err");
    expect(commandOutput(of("out"), of(""))).toBe("out");
    expect(commandOutput(of(""), of(""))).toBe("");
  });

  it("decodes invalid UTF-8 as Python's errors='replace', and keeps a BOM", () => {
    expect(commandOutput(windowOf(Buffer.from([0xff, 0x41]), 1), windowOf(Buffer.alloc(0), 2))).toBe("�A");
    expect(commandOutput(windowOf(Buffer.from([0xef, 0xbb, 0xbf, 0x41]), 1), windowOf(Buffer.alloc(0), 2))).toBe("﻿A");
  });

  it("keeps a text that fits, and caps one that does not to the head and tail", () => {
    expect(capText("short")).toBe("short");
    const capped = capText(`HEAD${"x".repeat(400_000)}TAIL`);
    expect(capped.startsWith("HEAD") && capped.endsWith("TAIL")).toBe(true);
    expect(capped).toContain("chars omitted by the computer");
    expect(pyJsonLength(capped)).toBeLessThan(OUTPUT_CAP_CHARS + 200);
  });

  it("never splits a character in two", () => {
    // With the u flag a pair is one code point, so only a lone surrogate matches.
    expect(/[\uD800-\uDFFF]/u.test(capText("😀".repeat(300_000)))).toBe(false);
  });
});

describe("the text of a process outcome", () => {
  it("strips terminal escapes as the cloud's strip_ansi does", () => {
    // Each answer is surogates/tools/utils/ansi_strip.py's on master.
    const cases: [string, string][] = [
      ["\x1b[31mred\x1b[0m", "red"],
      ["\x1b]0;title\x07x", "x"],
      ["\x1b]8;;http://a\x1b\\link\x1b]8;;\x1b\\", "link"],
      ["\x1b]never", "never"],
      ["\x1bPq#0\x1b\\done", "done"],
      ["\x9b1mbold", "bold"],
      ["a\x85b", "ab"],
      ["\x1b(Bx", "x"],
      ["\x1b", "\x1b"],
      ["\x1b ", "\x1b "],
      ["\x1b[12", "12"],
      ["é😀", "é😀"],
    ];
    for (const [text, stripped] of cases) expect(stripAnsi(text)).toBe(stripped);
  });

  it("strips escapes that never end in one pass", () => {
    const begun = Date.now();
    expect(stripAnsi("\x1b]".repeat(100_000))).toBe("");
    expect(Date.now() - begun).toBeLessThan(1_000);
  });

  it("splits lines as Python's splitlines does", () => {
    expect(splitLines("a\r\nb\rc\x0bd\x0ce\x1cf\x1dg\x1eh\x85i\u2028j\u2029k\n")).toEqual(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]);
    expect(splitLines("")).toEqual([]);
    expect(splitLines("\n")).toEqual([""]);
    expect(splitLines("a\n\nb")).toEqual(["a", "", "b"]);
  });

  it("slices in code points, as Python does", () => {
    expect(lastPoints("a😀b😀", 2)).toBe("b😀");
    expect(lastPoints("ab", 5)).toBe("ab");
    expect(firstPoints("😀😀x", 2)).toBe("😀😀");
  });

  it("caps every string at any depth, and makes it well-formed", () => {
    const capped = capStrings({ a: ["\ud800x", 1, null], b: { c: "x".repeat(300_000) } }) as { a: unknown[]; b: { c: string } };
    expect(capped.a).toEqual(["\ufffdx", 1, null]);
    expect(capped.b.c).toBe(capText("x".repeat(300_000)));
  });
});
