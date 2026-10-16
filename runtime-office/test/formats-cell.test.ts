import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { cell, compileCellOps } from "../src/formats/cell.js";
import { ContentError } from "../src/markdown.js";

const one = (op: Record<string, unknown>) => compileCellOps([op])[0];

describe("a spreadsheet's reads", () => {
  it("asks for the outline, a range or a search", () => {
    expect(cell.readScope({})).toEqual({ action: "outline" });
    expect(cell.readScope({ view: "range", target: "Sales!a1:d20" })).toEqual({ action: "range", sheet: "Sales", range: "A1:D20" });
    expect(cell.readScope({ view: "range", target: "'Q3 plan'!B2" })).toEqual({ action: "range", sheet: "Q3 plan", range: "B2" });
    expect(cell.readScope({ view: "search", query: "North" })).toEqual({ action: "search", query: "North" });
  });

  it("names what a spreadsheet reads, and how big a view may be", () => {
    expect(() => cell.readScope({ view: "block", target: "1" })).toThrow('In a spreadsheet "view" is "outline", "range"');
    expect(() => cell.readScope({ view: "range", target: "A1:B2" })).toThrow("with its sheet");
    expect(() => cell.readScope({ view: "range", target: "Big!A1:ZZ1000" })).toThrow("at most 50000 cells");
  });
});

describe("set", () => {
  it("sizes a range from its top-left cell and the values", () => {
    expect(one({ op: "set", range: "Sales!A6", values: [["West", 50, 60, "=B6+C6"], ["Sum", "=SUM(B2:B6)"]] })).toEqual({
      op: "set", sheet: "Sales", range: "A6:D7", values: [["West", 50, 60, "=B6+C6"], ["Sum", "=SUM(B2:B6)", "", ""]],
    });
  });

  it("writes an empty cell for null, never the editor's #N/A", () => {
    expect(one({ op: "set", range: "Sales!A1", values: [[null, true]] })).toMatchObject({ values: [["", true]] });
  });

  it("holds a whole range to its values' size", () => {
    expect(() => one({ op: "set", range: "Sales!A1:B2", values: [["a"]] })).toThrow("A1:B2 is 2 rows by 2 columns");
  });

  it("keeps the expected cells as text, sized like the range", () => {
    expect(one({ op: "set", range: "Sales!B2:C2", values: [[1, 2]], expect: [["120", 140]] })).toMatchObject({ expect: [["120", "140"]] });
    expect(() => one({ op: "set", range: "Sales!B2:C2", values: [[1, 2]], expect: [["120"]] })).toThrow('"expect" is the range as you read it');
  });

  it("refuses what is no cell value", () => {
    expect(() => one({ op: "set", range: "Sales!A1", values: [[{ a: 1 }]] })).toThrow("text, numbers or true/false");
    expect(() => one({ op: "set", range: "Sales!A1", values: [] })).toThrow('"values" is rows');
  });
});

describe("format, chart and sheet", () => {
  it("takes the named styles only", () => {
    expect(one({ op: "format", range: "Sales!A1:D1", bold: true, fill: "#E6F0FF", width: 18 })).toEqual({
      op: "format", sheet: "Sales", range: "A1:D1", style: { bold: true, fill: "#E6F0FF", width: 18 },
    });
    expect(() => one({ op: "format", range: "Sales!A1", colour: "#000000" })).toThrow('not "colour"');
    expect(() => one({ op: "format", range: "Sales!A1", fill: "blue" })).toThrow('"fill" is "none" or a colour like #1F4E79');
    expect(() => one({ op: "format", range: "Sales!A1" })).toThrow("says what to change");
  });

  it("anchors a chart on its range's sheet unless told otherwise", () => {
    expect(one({ op: "chart", range: "Sales!A1:C4", type: "column", title: "By region", anchor: "F2" })).toEqual({
      op: "chart", sheet: "Sales", range: "A1:C4", type: "column", title: "By region", on: "Sales", anchor: { row: 1, col: 5 },
      name: null, width: 480, height: 288,
    });
    expect(() => one({ op: "chart", range: "Sales!A1:C4", type: "radar", anchor: "F2" })).toThrow('"type" is one of column, bar, line');
  });

  it("changes a sheet once per call, and writes to none it deletes or renames away", () => {
    expect(one({ op: "sheet", action: "add", name: "Plan", after: "Sales" })).toEqual({ op: "sheet", action: "add", name: "Plan", after: "Sales" });
    expect(() => compileCellOps([{ op: "sheet", action: "add", name: "Plan" }, { op: "sheet", action: "rename", name: "Plan", to: "Budget" }]))
      .toThrow("op 1: one call changes a sheet once");
    expect(() => compileCellOps([{ op: "sheet", action: "delete", name: "Notes" }, { op: "set", range: "Notes!A1", values: [["x"]] }]))
      .toThrow("op 1: this call deletes or renames Notes");
    expect(() => one({ op: "sheet", action: "add", name: "Bad/Name" })).toThrow("without [ ] : * ? / \\");
  });

  it("refuses an unknown op", () => {
    expect(() => compileCellOps([{ op: "insert" }])).toThrow(ContentError);
  });
});

describe("a spreadsheet batch", () => {
  it("stops at a refusal the editor gives while asking for the cells", async () => {
    const refused = { refused: { op: 0, reason: "There is no sheet Gone: read the outline again." } };
    const run = vi.fn(async (scope: Record<string, unknown>) => (scope.action === "lock" ? refused : { results: {} }));
    const ops = compileCellOps([{ op: "set", range: "Gone!A1", values: [["x"]] }]);
    expect(await cell.edit({ page: {} as EditorPage, run }, ops)).toEqual(refused);
    expect(run.mock.calls.map(([scope]) => scope.action)).toEqual(["lock"]);
  });
});
