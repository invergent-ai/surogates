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
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "approved" },
    ])).toEqual([
      { op: "pages", action: "add", page: 1 },
      { op: "pages", action: "rotate", page: 2, degrees: 90 },
      { op: "fill", fields: { client: "ACME Lisbon Lda", signed: true } },
      { op: "add_field", type: "text", page: 1, rect: [170, 250, 420, 270], name: "po_number" },
      { op: "annotate", kind: "stamp", page: 1, rect: [300, 360, 450, 410], stamp: "SB_Approved" },
    ]);
    expect(compilePdfOps([{ op: "redact", text: "thirty" }])).toEqual([{ op: "redact", text: "thirty", match_case: false, whole_words: false }]);
  });

  it("sends a redaction in a call of its own", () => {
    const own = "send each redaction as a call of its own";
    expect(() => compilePdfOps([{ op: "redact", text: "thirty" }, { op: "pages", action: "add", page: 1 }])).toThrow(own);
    expect(() => compilePdfOps([{ op: "redact", text: "thirty" }, { op: "redact", text: "supplier" }])).toThrow(own);
  });

  it("checks a fill's expected values against its fields", () => {
    expect(compilePdfOps([{ op: "fill", fields: { client: "ACME", signed: true }, expect: { client: null, signed: false } }])).toEqual([
      { op: "fill", fields: { client: "ACME", signed: true }, expect: { client: "", signed: false } },
    ]);
    expect(() => compilePdfOps([{ op: "fill", fields: { client: "ACME" }, expect: { signed: false } }])).toThrow('"expect" names only fields in "fields"');
    expect(() => compilePdfOps([{ op: "fill", fields: { client: "ACME" }, expect: { client: true } }])).toThrow("expect client as text");
    expect(() => compilePdfOps([{ op: "fill", fields: { signed: true }, expect: { signed: "Off" } }])).toThrow("expect signed as true or false");
    expect(() => compilePdfOps([{ op: "fill", fields: { client: "ACME" }, expect: ["x"] }])).toThrow('"expect" maps field names');
  });

  it("names what is wrong", () => {
    expect(() => compilePdfOps([{ op: "pages", action: "rotate", page: 1, degrees: 45 }])).toThrow('"degrees" is 0, 90, 180 or 270');
    expect(() => compilePdfOps([{ op: "add_field", type: "text", page: 1, rect: [420, 250, 170, 270], name: "x" }])).toThrow('"rect" is [x1, y1, x2, y2]');
    expect(() => compilePdfOps([{ op: "annotate", kind: "stamp", page: 1, rect: [1, 1, 2, 2], stamp: "lovely" }])).toThrow('"stamp" is one of approved');
    expect(() => compilePdfOps([{ op: "replace", find: "a", text: "b" }])).toThrow("existing text cannot be changed");
  });
});

describe("a PDF's comment threads", () => {
  it("finds a mention's thread by its words and its author", () => {
    expect(pdf.readScope({ view: "thread", query: "@Agent is March right?", author: "user:1" })).toEqual({
      action: "thread", query: "@Agent is March right?", author: "user:1",
    });
    expect(() => pdf.readScope({ view: "thread", query: "x" })).toThrow('"author" is required');
  });

  it("takes a reply and a resolve, in a call of their own", () => {
    expect(compilePdfOps([{ op: "reply", thread: "1_1", body: "March." }, { op: "resolve", thread: "1_1" }])).toEqual([
      { op: "reply", thread: "1_1", body: "March." },
      { op: "resolve", thread: "1_1" },
    ]);
    expect(() => compilePdfOps([{ op: "resolve", thread: "1_1" }, { op: "pages", action: "add", page: 1 }])).toThrow("reply and resolve go in a call of their own");
    expect(() => compilePdfOps([{ op: "reply", thread: "1_1", body: "x".repeat(401) }])).toThrow("a thread reply states just the result");
  });
});
