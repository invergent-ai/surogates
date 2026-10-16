import { describe, expect, it } from "vitest";
import { word } from "../src/formats/word.js";
import { ContentError } from "../src/markdown.js";

describe("Word's reads", () => {
  it("asks the script for an outline page, a block or a search", () => {
    expect(word.readScope({})).toEqual({ action: "outline", start: 0, limit: 200 });
    expect(word.readScope({ view: "outline", start: 3, limit: 500 })).toEqual({ action: "outline", start: 3, limit: 200 });
    expect(word.readScope({ view: "block", target: "12" })).toEqual({ action: "view", target: "12" });
    expect(word.readScope({ view: "search", query: "revenue" })).toEqual({ action: "search", query: "revenue" });
  });

  it("names what a Word document reads", () => {
    expect(() => word.readScope({ view: "range", target: "A1" })).toThrow(ContentError);
    expect(() => word.readScope({ view: "block" })).toThrow('"target" is required.');
    expect(() => word.readScope({ view: "outline", start: -1 })).toThrow('"start" and "limit" are whole numbers.');
  });
});
