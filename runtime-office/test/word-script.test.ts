import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("the editor script", () => {
  it("passes the editor's check on command code: it never says import", () => {
    const script = readFileSync(new URL("../editor/word.js", import.meta.url), "utf8");
    expect(script).not.toMatch(/\bimport\s*[\s(]/i);
  });
});
