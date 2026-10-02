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
    ["\u{e0001}", "'\\U000e0001'"],
    ["\u00a0", "'\\xa0'"],
    ["\u202f", "'\\u202f'"],
    ["\ud800", "'\\ud800'"],
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
    ["\t", 4],
    ["\r", 4],
    ["\b", 4],
    ["\f", 4],
    ["\u2028", 8],
    ["\ud800", 8],
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

  it("pass a Failure through unchanged, even one that also looks like a Node error", () => {
    const failure = Object.assign(osError("EACCES", "/x"), { code: "ENOENT", errno: -2 });
    let caught: unknown;
    try {
      io("/y", () => { throw failure; });
    } catch (error) {
      caught = error;
    }
    expect(caught).toBe(failure);
    expect((caught as Failure).refusal.message).toBe("Permission denied: '/x'");
  });

  it("leave an error that is not a Node fs error as it is", () => {
    const plain = new Error("boom");
    expect(() => io("/y", () => { throw plain; })).toThrow(plain);
  });

  // The strings are Python's os.strerror, which is glibc's.
  it.each([
    ["EILSEQ", -84, "Invalid or incomplete multibyte or wide character"],
    ["ERANGE", -34, "Numerical result out of range"],
    ["ENOTCONN", -107, "Transport endpoint is not connected"],
    ["EHOSTUNREACH", -113, "No route to host"],
    ["ENOLCK", -37, "No locks available"],
    ["EUCLEAN", -117, "Structure needs cleaning"],
    ["ENOMEDIUM", -123, "No medium found"],
  ])("word %s as glibc does, where libuv words it differently or not at all", (code, errno, text) => {
    const error = Object.assign(new Error("fs"), { code, errno });
    expect(() => io("/y", () => { throw error; })).toThrow(
      expect.objectContaining({ refusal: { type: "os", code, message: `${text}: '/y'` } }),
    );
  });

  it("take libuv's text, capitalised, for a code the table lacks", () => {
    const error = Object.assign(new Error("fs"), { code: "EPIPE", errno: -32 });
    expect(() => io("/y", () => { throw error; })).toThrow(
      expect.objectContaining({ refusal: { type: "os", code: "EPIPE", message: "Broken pipe: '/y'" } }),
    );
  });
});
