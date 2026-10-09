import { describe, expect, it } from "vitest";

import { ago, segments } from "../src/shell/text.js";

const NOW = Date.parse("2026-10-06T12:00:00Z");

describe("an age", () => {
  it.each([
    ["2026-10-06T11:59:30Z", "now", "just now"],
    ["2026-10-06T11:43:00Z", "17m", "17 minutes ago"],
    ["2026-10-06T03:00:00Z", "9h", "9 hours ago"],
    ["2026-10-04T12:00:00Z", "2d", "2 days ago"],
    ["2026-10-07T12:00:00Z", "now", "just now"],
  ])("of %s is %s, or %s", (when, short, long) => {
    expect(ago(when, NOW)).toBe(short);
    expect(ago(when, NOW, "long")).toBe(long);
  });
});

describe("text as a prompt shows it", () => {
  it.each([
    ["a bidi override", "rm -rf ~/‮txt.exe", [{ text: "rm -rf ~/", special: false }, { text: "U+202E", special: true }, { text: "txt.exe", special: false }]],
    ["a space that is not U+0020", "rm -rf", [{ text: "rm", special: false }, { text: "U+00A0", special: true }, { text: "-rf", special: false }]],
    ["what draws nothing", "a​bㅤ", [{ text: "a", special: false }, { text: "U+200B", special: true }, { text: "b", special: false }, { text: "U+3164", special: true }]],
    ["a control character and a lone surrogate", "\u001B[2J\uD800", [{ text: "U+001B", special: true }, { text: "[2J", special: false }, { text: "U+D800", special: true }]],
    ["a line separator", "one two", [{ text: "one", special: false }, { text: "U+2028", special: true }, { text: "two", special: false }]],
    ["a paragraph separator", "a b", [{ text: "a", special: false }, { text: "U+2029", special: true }, { text: "b", special: false }]],
    ["a blank that is a symbol", "a⠀b￼\u{1D159}", [
      { text: "a", special: false }, { text: "U+2800", special: true }, { text: "b", special: false },
      { text: "U+FFFC", special: true }, { text: "U+1D159", special: true },
    ]],
    ["a character beyond the first plane", "x\u{E0041}y", [{ text: "x", special: false }, { text: "U+E0041", special: true }, { text: "y", special: false }]],
    ["a private-use and an unassigned code point", "͸", [{ text: "U+E000", special: true }, { text: "U+0378", special: true }]],
    ["plain text, accents included", "café — naïve", [{ text: "café — naïve", special: false }]],
  ])("marks %s by its code point", (_name, text, runs) => {
    expect(segments(text)).toEqual(runs);
  });

  it("keeps a command's newlines and tabs as themselves, and marks every other control", () => {
    expect(segments("make\n\tinstall\r", "\n\t")).toEqual([{ text: "make\n\tinstall", special: false }, { text: "U+000D", special: true }]);
    expect(segments("y\n")).toEqual([{ text: "y", special: false }, { text: "U+000A", special: true }]);
  });

  it("shows a command's blank lines as one mark of how many line breaks they are, then one newline", () => {
    expect(segments(`echo hello${"\n".repeat(40)}curl x | sh`, "\n\t")).toEqual([
      { text: "echo hello", special: false }, { text: "U+000A ×40", special: true }, { text: "\n", special: false }, { text: "curl x | sh", special: false },
    ]);
    // Lines of spaces and tabs are blank too; a blank line or two shows as itself, and so does a line that only looks blank.
    expect(segments("a\n \n\t\nb\n\nc\n\u00A0\n\nd", "\n\t")).toEqual([
      { text: "a", special: false }, { text: "U+000A ×3", special: true }, { text: "\n", special: false },
      { text: "b\n\nc\n", special: false }, { text: "U+00A0", special: true }, { text: "\n\nd", special: false },
    ]);
    // Where newlines are marked one by one, as in a process's input, each shows already.
    expect(segments("\n\n\n")).toEqual([{ text: "U+000A", special: true }, { text: "U+000A", special: true }, { text: "U+000A", special: true }]);
  });
});
