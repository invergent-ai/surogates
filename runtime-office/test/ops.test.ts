import { describe, expect, it } from "vitest";
import { ContentError } from "../src/markdown.js";
import { compileOps } from "../src/ops.js";

const block = { block: "853", hash: "a1b2c3d4" };

describe("compileOps", () => {
  it("compiles each op, with the content's markdown as blocks", () => {
    expect(
      compileOps([
        { op: "insert", target: block, side: "before", content: { markdown: "Hello." } },
        { op: "insert", target: { end: true }, content: { markdown: "End." } },
        { op: "replace", target: { pending: "s" }, content: { markdown: "## Summary" } },
        { op: "replace", target: { find: "revenue target" }, content: { text: "sales goal" } },
        { op: "delete", target: { find: "old words", nth: 2 } },
        { op: "move", target: block, to: { end: true } },
        { op: "comment", anchor: { find: "North grew" }, body: "Source?" },
        { op: "reply", thread: "77", body: "Added." },
        { op: "resolve", thread: "77" },
      ]),
    ).toEqual([
      { op: "insert", target: block, side: "before", blocks: [{ t: "p", runs: [{ text: "Hello." }] }] },
      { op: "insert", target: { end: true }, side: "after", blocks: [{ t: "p", runs: [{ text: "End." }] }] },
      { op: "replace", target: { pending: "s" }, blocks: [{ t: "h", level: 2, runs: [{ text: "Summary" }] }] },
      { op: "reword", target: { find: "revenue target", nth: 1 }, text: "sales goal" },
      { op: "delete", target: { find: "old words", nth: 2 } },
      { op: "move", target: block, to: { end: true }, side: "after" },
      { op: "comment", anchor: { find: "North grew", nth: 1 }, body: "Source?" },
      { op: "reply", thread: "77", body: "Added." },
      { op: "resolve", thread: "77" },
    ]);
  });

  it("takes a thread id given as a number", () => {
    expect(compileOps([{ op: "reply", thread: 1184659669, body: "Done." }, { op: "resolve", thread: 1184659669 }])).toEqual([
      { op: "reply", thread: "1184659669", body: "Done." },
      { op: "resolve", thread: "1184659669" },
    ]);
    expect(() => compileOps([{ op: "resolve" }])).toThrow(/"thread" is required/);
  });

  it("refuses an empty or oversized batch", () => {
    expect(() => compileOps([])).toThrow(ContentError);
    expect(() => compileOps("insert")).toThrow(ContentError);
    expect(() => compileOps(Array.from({ length: 51 }, () => ({ op: "delete", target: block })))).toThrow(/at most 50/);
  });

  it("names the op it refuses", () => {
    expect(() => compileOps([{ op: "delete", target: block }, { op: "paint", target: block }])).toThrow(/^op 1: unknown op/);
    expect(() => compileOps([{ op: "delete", target: block }, "delete"])).toThrow(/^op 1: an op is an object\.$/);
  });

  it("refuses targets that cannot work", () => {
    expect(() => compileOps([{ op: "delete", target: { end: true } }])).toThrow(/place to insert/);
    expect(() => compileOps([{ op: "delete", target: { block: "1" } }])).toThrow(/hash/);
    expect(() => compileOps([{ op: "delete", target: { find: "x", nth: 0 } }])).toThrow(/from 1/);
    expect(() => compileOps([{ op: "delete", target: { find: "  " } }])).toThrow(ContentError);
    expect(() => compileOps([{ op: "delete", target: {} }])).toThrow(/a target is/);
  });

  it("matches the content to the target", () => {
    expect(() => compileOps([{ op: "replace", target: block, content: { text: "x" } }])).toThrow(/re-words quoted words/);
    expect(() => compileOps([{ op: "replace", target: { find: "x" }, content: { markdown: "y" } }])).toThrow(/text/);
    expect(() => compileOps([{ op: "insert", target: block, content: {} }])).toThrow(/markdown/);
    expect(() => compileOps([{ op: "move", target: { find: "x" }, to: block }])).toThrow(/not quoted words/);
    expect(() => compileOps([{ op: "comment", anchor: block, body: "x" }])).toThrow(/quoted words/);
    expect(() => compileOps([{ op: "insert", target: block, side: "above", content: { markdown: "y" } }])).toThrow(/side/);
  });
});
