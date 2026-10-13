import { describe, expect, it } from "vitest";
import { compileContent, ContentError } from "../src/markdown.js";

describe("compileContent", () => {
  it("maps headings, paragraphs and inline styles", () => {
    expect(compileContent("# Title\n\nSome **bold** and *italic* with `code` and [a link](https://x.test).")).toEqual([
      { t: "h", level: 1, runs: [{ text: "Title" }] },
      {
        t: "p",
        runs: [
          { text: "Some " }, { text: "bold", b: true }, { text: " and " }, { text: "italic", i: true },
          { text: " with " }, { text: "code", code: true }, { text: " and " },
          { text: "a link", href: "https://x.test" }, { text: "." },
        ],
      },
    ]);
  });

  it("writes headings deeper than three as level three", () => {
    expect(compileContent("#### Deep")).toEqual([{ t: "h", level: 3, runs: [{ text: "Deep" }] }]);
  });

  it("gives each list its own numbering and nests levels", () => {
    expect(compileContent("- a\n- b\n  - c\n\n1. one\n2. two")).toEqual([
      { t: "li", list: 0, ordered: false, level: 0, runs: [{ text: "a" }] },
      { t: "li", list: 0, ordered: false, level: 0, runs: [{ text: "b" }] },
      { t: "li", list: 0, ordered: false, level: 1, runs: [{ text: "c" }] },
      { t: "li", list: 1, ordered: true, level: 0, runs: [{ text: "one" }] },
      { t: "li", list: 1, ordered: true, level: 0, runs: [{ text: "two" }] },
    ]);
  });

  it("keeps a table's header as its first row", () => {
    expect(compileContent("| A | B |\n|---|---|\n| 1 | **2** |")).toEqual([
      { t: "table", rows: [[[{ text: "A" }], [{ text: "B" }]], [[{ text: "1" }], [{ text: "2", b: true }]]] },
    ]);
  });

  it("keeps a code block's lines", () => {
    expect(compileContent("```\nx = 1\ny = 2\n```")).toEqual([{ t: "code", lines: ["x = 1", "y = 2"] }]);
  });

  it("places typed blocks between the markdown around them", () => {
    expect(compileContent("## Risks\n<?block r?>\nAfter.", { r: { type: "pending", intent: "What could slip" } })).toEqual([
      { t: "h", level: 2, runs: [{ text: "Risks" }] },
      { t: "pending", key: "r", intent: "What could slip" },
      { t: "p", runs: [{ text: "After." }] },
    ]);
  });

  it("refuses a placeholder no block answers, and a block of an unknown type", () => {
    expect(() => compileContent("<?block x?>")).toThrow(ContentError);
    expect(() => compileContent("<?block x?>", { x: { type: "video" } })).toThrow(ContentError);
    expect(() => compileContent("<?block x?>", { x: { type: "pending" } })).toThrow(/intent/);
  });

  it("turns what it does not map into plain text or nothing", () => {
    expect(compileContent("![a chart](https://x.test/c.png)\n\n<div>raw</div>\n\n> > quoted\n\n---")).toEqual([
      { t: "p", runs: [{ text: "a chart" }] },
      { t: "p", runs: [{ text: "quoted" }] },
    ]);
  });

  it("refuses more than 400 blocks in one op", () => {
    expect(() => compileContent("p\n\n".repeat(401))).toThrow(ContentError);
  });
});
