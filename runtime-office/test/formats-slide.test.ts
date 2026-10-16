import { describe, expect, it, vi } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { EditorError } from "../src/formats/format.js";
import { checkSlideOps, slideFormat } from "../src/formats/slide.js";
import { ContentError } from "../src/markdown.js";
import type { Canvas, Measurer } from "../src/slides/measure.js";

const spec = { background: null, shapes: [], notes: null, hidden: false, transition: null };

describe("a deck's reads", () => {
  const format = slideFormat({} as Measurer);

  it("asks for the outline, a slide or a search", () => {
    expect(format.readScope({})).toEqual({ action: "outline" });
    expect(format.readScope({ view: "slide", target: "1_402" })).toEqual({ action: "slide", target: "1_402" });
    expect(format.readScope({ view: "search", query: "Lisbon" })).toEqual({ action: "search", query: "Lisbon" });
    expect(() => format.readScope({ view: "range", target: "A1" })).toThrow('In a slide deck "view" is "outline", "slide"');
  });
});

describe("slide ops", () => {
  it("adds after a slide, or replaces one by id with its hash", () => {
    const ops = [{ op: "slides", slides: [{ html: "<section></section>", after: "1428" }, { id: "1473", hash: "ab12cd34", html: "<section></section>" }] }];
    expect(checkSlideOps(ops)).toEqual(ops);
    expect(() => checkSlideOps([{ op: "slides", slides: [{ id: "1473", html: "<section></section>" }] }])).toThrow("carries the hash");
  });

  it("orders in a call of its own, and removes by id and hash", () => {
    expect(checkSlideOps([{ op: "order", ids: ["2", "1"] }])).toEqual([{ op: "order", ids: ["2", "1"] }]);
    expect(() => checkSlideOps([{ op: "order", ids: ["1", "1"] }])).toThrow("each slide once");
    expect(() => checkSlideOps([{ op: "slides", slides: [{ html: "<section></section>" }] }, { op: "order", ids: ["1"] }])).toThrow("order slides in a call of its own");
    expect(() => checkSlideOps([{ op: "remove", slides: [{ id: "1" }] }])).toThrow("carries the hash");
  });

  it("changes one shape's text by its slide's id and hash and its own id, a line per paragraph", () => {
    expect(checkSlideOps([{ op: "text", slide: "1473", hash: "dc3285c8", shape: "1482", text: "Q3 plan\r\nfor Lisbon" }]))
      .toEqual([{ op: "text", slide: "1473", hash: "dc3285c8", shape: "1482", text: "Q3 plan\nfor Lisbon" }]);
    expect(checkSlideOps([{ op: "text", slide: 1473, hash: "h", shape: 1482, text: "" }])).toEqual([{ op: "text", slide: "1473", hash: "h", shape: "1482", text: "" }]);
    expect(() => checkSlideOps([{ op: "text", slide: "1473", shape: "1482", text: "x" }])).toThrow("op 0: a shape's text change carries the hash its slide had when you read it.");
    expect(() => checkSlideOps([{ op: "text", hash: "h", shape: "1482", text: "x" }])).toThrow('"slide" is required');
    expect(() => checkSlideOps([{ op: "text", slide: "1473", hash: "h", text: "x" }])).toThrow('"shape" is required');
    expect(() => checkSlideOps([{ op: "text", slide: "1473", hash: "h", shape: "1482" }])).toThrow('"text" is the shape\'s new text, a line per paragraph.');
    expect(() => checkSlideOps([{ op: "text", slide: "1473", hash: "h", shape: "1482", text: "x".repeat(2001) }])).toThrow('"text" is at most 2000 characters');
    expect(() => checkSlideOps([{ op: "rename" }])).toThrow('unknown op "rename"; use slides, text, order, remove, reply or resolve.');
  });

  it("sends a text change to the editor as it is: there is nothing to measure", async () => {
    const measure = vi.fn();
    const format = slideFormat({ measure } as unknown as Measurer);
    const context = { page: {} as EditorPage, run: vi.fn() };
    const op = { op: "text", slide: "1473", hash: "dc3285c8", shape: "1482", text: "Q3 plan" };
    expect(await format.compile([op], context)).toEqual([op]);
    expect(context.run).not.toHaveBeenCalled();
    expect(measure).not.toHaveBeenCalled();
  });

  it("measures at most ten slides a call", () => {
    const slides = Array.from({ length: 11 }, () => ({ html: "<section></section>" }));
    expect(() => checkSlideOps([{ op: "slides", slides }])).toThrow("at most 10 slides");
  });

  it("measures each slide on the deck's own canvas, and names the slide a refusal is about", async () => {
    const measure = vi.fn(async (html: string, _canvas?: Canvas) => {
      if (html.includes("too big")) throw new ContentError("The content of <section > p> overflows its box by 40 px.");
      return spec;
    });
    const format = slideFormat({ measure } as unknown as Measurer);
    const context = { page: {} as EditorPage, run: vi.fn(async () => ({ width: 9144000, height: 6858000 })) };
    expect(await format.compile([{ op: "slides", slides: [{ html: "<section>a</section>" }] }], context)).toEqual([{ op: "slides", slides: [{ after: null, spec }] }]);
    expect(measure.mock.calls[0]![1]).toEqual({ width: 1920, height: 1440, emuPerPx: 4762.5 });
    await expect(format.compile([{ op: "slides", slides: [{ html: "<section>ok</section>" }, { html: "<section>too big</section>" }] }], context))
      .rejects.toThrow("op 0, slide 1: The content of <section > p> overflows its box by 40 px.");
  });

  it("says a slide that could not be measured for a reason not its own is the editor's failure, in one line", async () => {
    const measure = vi.fn(async () => {
      throw new Error("page.screenshot: Timeout 30000ms exceeded.\nCall log:\n  - taking page screenshot");
    });
    const format = slideFormat({ measure } as unknown as Measurer);
    const context = { page: {} as EditorPage, run: vi.fn(async () => ({ width: 12192000, height: 6858000 })) };
    const compiled = format.compile([{ op: "slides", slides: [{ html: "<section>a</section>" }] }], context);
    await expect(compiled).rejects.toThrow(EditorError);
    await expect(compiled).rejects.toThrow(/^op 0, slide 0 could not be measured: page\.screenshot: Timeout 30000ms exceeded\.$/);
  });
});

describe("a deck's comment threads", () => {
  const thread = "{9D24FA3F-798C-22EB-4B89-84EF55C02C52}";

  it("takes a reply and a resolve, in a call of their own", () => {
    expect(checkSlideOps([{ op: "reply", thread, body: "March." }, { op: "resolve", thread }])).toEqual([
      { op: "reply", thread, body: "March." },
      { op: "resolve", thread },
    ]);
    expect(() => checkSlideOps([{ op: "resolve", thread }, { op: "order", ids: ["1_402"] }])).toThrow("reply and resolve go in a call of their own");
    expect(() => checkSlideOps([{ op: "reply", thread, body: "x".repeat(401) }])).toThrow("a thread reply states just the result");
  });
});
