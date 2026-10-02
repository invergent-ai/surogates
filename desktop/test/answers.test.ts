import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { Failure, io, osError, pyJsonLength, pyRepr } from "../src/files/answers.js";

describe("pyRepr, as Python's repr() of a str", () => {
  it.each([
    ["plain", "'plain'"],
    ["it's", `"it's"`],
    ['a"b', `'a"b'`],
    [`it's "x"`, `'it\\'s "x"'`],
    ["a\tb\nc\r", "'a\\tb\\nc\\r'"],
    ["a\\b", "'a\\\\b'"],
    ["é€😀", "'é€😀'"],
    ["\u200b", "'\\u200b'"],
    ["\x7f\x00", "'\\x7f\\x00'"],
    ["My Files ü.txt", "'My Files ü.txt'"],
  ])("%j", (text, repr) => {
    expect(pyRepr(text)).toBe(repr);
  });
});

describe("pyJsonLength, as len(json.dumps(text)) in Python", () => {
  it.each([
    ["", 2],
    ["a", 3],
    ['"', 4],
    ["\\", 4],
    ["\n", 4],
    ["\x01", 8],
    ["\x7f", 8],
    ["é", 8],
    ["😀", 14],
    ["\x00", 8],
  ])("%j costs %i", (text, length) => {
    expect(pyJsonLength(text)).toBe(length);
  });
});

describe("OS errors", () => {
  it("read as Python's str(OSError) without the errno prefix", () => {
    expect(osError("ENOENT", "/x/it's").refusal).toEqual({
      type: "os", code: "ENOENT", message: `No such file or directory: "/x/it's"`,
    });
    expect(osError("EISDIR", "/x").refusal.message).toBe("Is a directory: '/x'");
  });

  it("come from a Node fs error with the path they were about", () => {
    let caught: unknown;
    try {
      io("/no/such/file", () => readFileSync("/no/such/file"));
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(Failure);
    expect((caught as Failure).refusal).toEqual({
      type: "os", code: "ENOENT", message: "No such file or directory: '/no/such/file'",
    });
  });

  it("pass a Failure through unchanged", () => {
    const failure = osError("EACCES", "/x");
    expect(() => io("/y", () => { throw failure; })).toThrow(failure);
  });
});
