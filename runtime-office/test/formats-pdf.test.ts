import { describe, expect, it } from "vitest";
import { compilePdfOps, pdf } from "../src/formats/pdf.js";

describe("a PDF's reads", () => {
  it("asks for the outline, a page or a search", () => {
    expect(pdf.grouped).toBe(false);
    expect(pdf.readScope({})).toEqual({ action: "outline", start: 0, limit: 100 });
    expect(pdf.readScope({ view: "page", target: "2" })).toEqual({ action: "page", page: 2 });
    expect(pdf.readScope({ view: "search", query: "invoice" })).toEqual({ action: "search", query: "invoice" });
    expect(() => pdf.readScope({ view: "page", target: "0" })).toThrow('"target" is a whole number from 1');
    expect(() => pdf.readScope({ view: "block", target: "1" })).toThrow('In a PDF "view" is "outline", "page"');
  });
});

describe("PDF ops", () => {
  it("checks pages, fields, redactions and annotations", () => {
    expect(compilePdfOps([
      { op: "pages", action: "add", page: 1 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number" },
      { op: "redact", text: "thirty" },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "approved" },
    ])).toEqual([
      { op: "pages", action: "add", page: 1 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number" },
      { op: "redact", text: "thirty", match_case: false, whole_words: false },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "SB_Approved" },
    ]);
  });

  it("names what is wrong", () => {
    expect(() => compilePdfOps([{ op: "pages", action: "rotate", page: 1, degrees: 45 }])).toThrow('"degrees" is 0, 90, 180 or 270');
    expect(() => compilePdfOps([{ op: "add_field", type: "text", page: 1, rect: [420, 250, 170, 270], name: "x" }])).toThrow('"rect" is [x1, y1, x2, y2]');
    expect(() => compilePdfOps([{ op: "annotate", kind: "stamp", page: 1, rect: [1, 1, 2, 2], stamp: "lovely" }])).toThrow('"stamp" is one of approved');
    expect(() => compilePdfOps([{ op: "replace", find: "a", text: "b" }])).toThrow("existing text cannot be changed");
  });
});
