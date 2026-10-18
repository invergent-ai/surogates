import { readFileSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { compileCellOps } from "../src/formats/cell.js";
import { compilePdfOps } from "../src/formats/pdf.js";
import { checkSlideOps } from "../src/formats/slide.js";
import type { Block } from "../src/markdown.js";
import { compileOps } from "../src/ops.js";
import { Measurer, type Canvas, type Para, type ShapeSpec } from "../src/slides/measure.js";
import { chromiumThere, measuringPage, systemFonts } from "./support/chromium.js";

// The office skill's references teach the agent these ops, and its templates are calls the agent sends as they are:
// each example call and each template must still pass its format's checks.
type Raw = Record<string, unknown>;
type Check = (ops: unknown[]) => unknown;
const bundle = (folder: "references" | "templates") => (name: string) =>
  readFileSync(new URL(`../../skills/productivity/office/${folder}/${name}`, import.meta.url), "utf8");
const reference = bundle("references");
const template = bundle("templates");

// Each ```json block is one call, or one call or op per line.
function examples(text: string): Raw[] {
  return [...text.matchAll(/```json\n([\s\S]*?)```/g)].flatMap(([, block]) => {
    try {
      return [JSON.parse(block!) as Raw];
    } catch {
      return block!.trim().split("\n").map((line) => JSON.parse(line) as Raw);
    }
  });
}
// A call's ops, a lone op as a call of its own; a read has none.
const callsOf = (text: string): Raw[][] =>
  examples(text).flatMap((example) => (Array.isArray(example.ops) ? [example.ops as Raw[]] : example.op ? [[example]] : []));

// The agent names a picture by its workspace path; the worker reads it into data before the ops reach runtime-office.
// A picture here gets what the worker would make of a one-pixel stand-in.
const PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
function asTheWorkerSendsIt(op: Raw): Raw {
  if (op.op === "slides") {
    return { ...op, slides: (op.slides as Raw[]).map((s) => ({ ...s, html: String(s.html).replace(/(<img\b[^>]*?\bsrc=")([^"]+)"/g, `$1${PIXEL}"`) })) };
  }
  const content = op.content as Raw | undefined;
  const blocks = content?.blocks as Record<string, Raw> | undefined;
  if (!blocks) return op;
  const read = Object.fromEntries(Object.entries(blocks).map(([key, block]) =>
    [key, block.type === "image" ? { type: "image", data: "", mime: "image/png", width: block.width ?? 1, height: 1 } : block]));
  return { ...op, content: { ...content, blocks: read } };
}

function eachAccepted(calls: Raw[][], check: Check) {
  it.each(calls.map((ops, index) => [index, ops.map((op) => op.op).join(", "), ops] as const))("call %i (%s) is accepted", (_index, _names, ops) => {
    expect(() => check(ops.map(asTheWorkerSendsIt))).not.toThrow();
  });
}

// Each reference's examples: at least so many ops.
const CHECKS: [string, Check, number][] = [
  ["docx.md", compileOps, 10],
  ["xlsx.md", compileCellOps, 6],
  ["pptx.md", checkSlideOps, 3],
  ["pdf.md", compilePdfOps, 5],
];

describe.each(CHECKS)("the office skill's %s examples", (name, check, least) => {
  const calls = callsOf(reference(name));

  it("are found", () => {
    expect(calls.flat().length).toBeGreaterThanOrEqual(least);
  });

  eachAccepted(calls, check);
});

// Each type's templates: at least so many calls.
const TEMPLATES: [string, Check, number][] = [
  ["docx.md", compileOps, 6],
  ["xlsx.md", compileCellOps, 3],
  ["pptx.md", checkSlideOps, 8],
];

describe.each(TEMPLATES)("the office skill's %s templates", (name, check, least) => {
  const calls = callsOf(template(name));

  it("are found", () => {
    expect(calls.length).toBeGreaterThanOrEqual(least);
  });

  eachAccepted(calls, check);
});

describe("the slide templates", () => {
  it("are eight calls, each one slide of one <section>", () => {
    const calls = callsOf(template("pptx.md"));
    expect(calls).toHaveLength(8);
    for (const ops of calls) {
      expect(ops.map((op) => op.op)).toEqual(["slides"]);
      const slides = ops[0]!.slides as Raw[];
      expect(slides).toHaveLength(1);
      expect(String(slides[0]!.html).match(/<section\b/g)).toHaveLength(1);
    }
  });
});

describe("the Word templates", () => {
  it("each plant a new document in one insert at its end, with sections still to write", () => {
    for (const ops of callsOf(template("docx.md"))) {
      expect(ops.map((op) => [op.op, op.target])).toEqual([["insert", { end: true }]]);
      const kinds = compileOps(ops).flatMap((op) => ("blocks" in op ? op.blocks : [])).map((block: Block) => block.t);
      expect(kinds).toContain("pending");
    }
  });
});

// office_edit's description shows a new Word document's outline as this call, word for word: the Python suite holds the
// description to this copy, and this test holds the copy to the Word checks.
describe("the office_edit description's Word call", () => {
  const call = JSON.parse(readFileSync(new URL("./edit-description-word.json", import.meta.url), "utf8")) as { ops: Raw[] };

  it("is accepted, as an outline whose sections are pending blocks under their headings", () => {
    const kinds = compileOps(call.ops).flatMap((op) => ("blocks" in op ? op.blocks : [])).map((block: Block) => block.t);
    expect(kinds.filter((kind) => kind === "pending").length).toBeGreaterThanOrEqual(2);
    kinds.forEach((kind, i) => {
      if (kind === "pending") expect(kinds[i - 1], kinds.join(", ")).toBe("h");
    });
  });
});

// A measurer on a Chromium page of its own, opened before the describe it is called in and closed after it.
function measuring(): () => Measurer {
  let opened: Awaited<ReturnType<typeof measuringPage>>;
  let measurer: Measurer;
  beforeAll(async () => {
    opened = await measuringPage();
    measurer = new Measurer(async () => opened.page, systemFonts);
  });
  afterAll(async () => {
    await opened.close();
  });
  return () => measurer;
}

describe.skipIf(!chromiumThere)("the slide reference's example slides", () => {
  const measurer = measuring();
  const slides = callsOf(reference("pptx.md")).flat().filter((op) => op.op === "slides").flatMap((op) => asTheWorkerSendsIt(op).slides as Raw[]);

  it("are found", () => {
    expect(slides.length).toBeGreaterThanOrEqual(2);
  });

  it.each(slides.map((slide, index) => [index, slide.html as string]))("slide %i measures", async (_index, html) => {
    await expect(measurer().measure(html, { width: 1920, height: 1080, emuPerPx: 6350 }, {} as EditorPage)).resolves.toBeTruthy();
  });

  it("show a magic move: the second slide arrives by it, and both pin the element that moves by one id", async () => {
    const at = slides.findIndex((slide) => /^<section data-transition="magic"/.test(String(slide.html))) - 1;
    expect(at).toBeGreaterThanOrEqual(0);
    const measure = (slide: Raw | undefined) => measurer().measure(String(slide?.html), { width: 1920, height: 1080, emuPerPx: 6350 }, {} as EditorPage);
    const first = await measure(slides[at]), second = await measure(slides[at + 1]);
    expect(first.transition).toBeNull();
    expect(second.transition?.effect).toBe("magic");
    const names = (shapes: { magic?: string }[]) => shapes.flatMap((s) => (s.magic ? [s.magic] : []));
    expect(names(first.shapes).filter((name) => names(second.shapes).includes(name))).toHaveLength(1);
  });
});

// A deck is 16:9 or 4:3: 1920 px wide either way, 1080 or 1440 tall, each in its own EMU per px, as slide.ts sizes the
// canvas from the deck. A template slide grows by flow, so one section fits both.
const CANVASES: [string, Canvas][] = [
  ["16:9", { width: 1920, height: 1080, emuPerPx: 12192000 / 1920 }],
  ["4:3", { width: 1920, height: 1440, emuPerPx: 9144000 / 1920 }],
];
// The templates' type scale, in px.
const SCALE = [128, 64, 44, 32, 24];

// Each run's size in px, from the points the measurer gives it on *canvas*.
function runSizes(shapes: ShapeSpec[], canvas: Canvas): number[] {
  const paras = (shape: ShapeSpec): Para[] => {
    if (shape.kind === "group") return shape.children.flatMap(paras);
    if (shape.kind === "shape") return shape.text?.paras ?? [];
    if (shape.kind === "table") return shape.rows.flatMap((row) => row.cells.flatMap((cell) => cell.paras));
    return [];
  };
  const ptPerPx = canvas.emuPerPx / 12700;
  return shapes.flatMap(paras).flatMap((para) => para.runs.map((run) => Math.round(run.size / ptPerPx)));
}

describe.skipIf(!chromiumThere)("the slide templates' slides", () => {
  const measurer = measuring();
  const slides = callsOf(template("pptx.md")).flat().flatMap((op) => op.slides as Raw[]);
  const cases = CANVASES.flatMap(([shape, canvas]) => slides.map((slide, index) => [index, shape, canvas, String(slide.html)] as const));

  // Measured: nothing overflows or reaches past the edge, no text reads under 3:1, no CSS is taken that the slide does
  // not keep, and every run is set in a size of the scale (a tag's own size drifting, or a stray font-size, shows here).
  it.each(cases)("slide %i measures on a %s deck, every style kept and every size of the scale", async (_index, _shape, canvas, html) => {
    const spec = await measurer().measure(html, canvas, {} as EditorPage);
    expect(spec.ignored).toEqual([]);
    const sizes = runSizes(spec.shapes, canvas);
    expect(sizes.length).toBeGreaterThan(0);
    expect(sizes.filter((px) => !SCALE.includes(px))).toEqual([]);
  });
});
