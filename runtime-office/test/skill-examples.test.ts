import { readFileSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { EditorPage } from "../src/connector.js";
import { compileCellOps } from "../src/formats/cell.js";
import { compilePdfOps } from "../src/formats/pdf.js";
import { checkSlideOps } from "../src/formats/slide.js";
import { compileOps } from "../src/ops.js";
import { Measurer } from "../src/slides/measure.js";
import { chromiumThere, measuringPage, systemFonts } from "./support/chromium.js";

// The office skill's references teach the agent these ops: each example call must still pass its format's checks.
type Raw = Record<string, unknown>;
const reference = (name: string) => readFileSync(new URL(`../../skills/productivity/office/references/${name}`, import.meta.url), "utf8");

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

const CHECKS: [string, (ops: unknown[]) => unknown, number][] = [
  ["word.md", compileOps, 10],
  ["cell.md", compileCellOps, 6],
  ["slide.md", checkSlideOps, 3],
  ["pdf.md", compilePdfOps, 5],
];

describe.each(CHECKS)("the office skill's %s examples", (name, check, least) => {
  const calls = callsOf(reference(name));

  it("are found", () => {
    expect(calls.flat().length).toBeGreaterThanOrEqual(least);
  });

  it.each(calls.map((ops, index) => [index, ops.map((op) => op.op).join(", "), ops] as const))("call %i (%s) is accepted", (_index, _names, ops) => {
    expect(() => check(ops.map(asTheWorkerSendsIt))).not.toThrow();
  });
});

describe.skipIf(!chromiumThere)("the slide reference's example slides", () => {
  let close: () => Promise<void>;
  let measurer: Measurer;
  beforeAll(async () => {
    const opened = await measuringPage();
    close = opened.close;
    measurer = new Measurer(async () => opened.page, systemFonts);
  });
  afterAll(async () => {
    await close();
  });

  const slides = callsOf(reference("slide.md")).flat().filter((op) => op.op === "slides").flatMap((op) => asTheWorkerSendsIt(op).slides as Raw[]);

  it("are found", () => {
    expect(slides.length).toBeGreaterThanOrEqual(2);
  });

  it.each(slides.map((slide, index) => [index, slide.html as string]))("slide %i measures", async (_index, html) => {
    await expect(measurer.measure(html, { width: 1920, height: 1080, emuPerPx: 6350 }, {} as EditorPage)).resolves.toBeTruthy();
  });
});
