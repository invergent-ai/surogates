import { readFileSync } from "node:fs";
import { ContentError } from "../markdown.js";
import type { Canvas, Measurer } from "../slides/measure.js";
import { checkBatch, EditorError, textArg, type EditContext, type Format, type Outcome, type Scope } from "./format.js";

const MAX_OPS = 20;
const MAX_SLIDES = 10;
const CANVAS_WIDTH = 1920;

type Raw = Record<string, unknown>;
export type SlideEntry = { html: string; after: string | null } | { id: string; hash: string; html: string };
export type SlideOp =
  | { op: "slides"; slides: SlideEntry[] }
  | { op: "order"; ids: string[] }
  | { op: "remove"; slides: { id: string; hash: string }[] };

function guarded(raw: unknown, what: string): { id: string; hash: string } {
  const entry = (raw ?? {}) as Raw;
  if (typeof entry.hash !== "string" || !entry.hash) throw new ContentError(`${what} carries the hash the slide had when you read it.`);
  return { id: textArg(entry.id, "id"), hash: entry.hash };
}

function checkOp(raw: Raw): SlideOp {
  switch (raw.op) {
    case "slides": {
      if (!Array.isArray(raw.slides) || !raw.slides.length) throw new ContentError('"slides" is a list of {"html": "<section>…</section>"}.');
      return {
        op: "slides",
        slides: raw.slides.map((input: unknown): SlideEntry => {
          const entry = (input ?? {}) as Raw;
          if (typeof entry.html !== "string" || !entry.html.trim()) throw new ContentError('each slide has "html": one <section>…</section> in the slide format.');
          if (entry.id !== undefined) return { ...guarded(entry, "a slide to replace"), html: entry.html };
          return { html: entry.html, after: entry.after == null ? null : textArg(entry.after, "after") };
        }),
      };
    }
    case "order": {
      if (!Array.isArray(raw.ids) || !raw.ids.length) throw new ContentError('"ids" lists the deck\'s slides by id, in the order wanted.');
      const ids = raw.ids.map((value) => textArg(value, "ids"));
      if (new Set(ids).size !== ids.length) throw new ContentError('"ids" names each slide once.');
      return { op: "order", ids };
    }
    case "remove": {
      if (!Array.isArray(raw.slides) || !raw.slides.length) throw new ContentError('"slides" lists the slides to remove: {"id", "hash"} each.');
      return { op: "remove", slides: raw.slides.map((entry) => guarded(entry, "a slide to remove")) };
    }
    default:
      throw new ContentError(`unknown op ${JSON.stringify(raw.op)}; use slides, order or remove.`);
  }
}

/** The agent's batch, checked as far as it can be without measuring. */
export function checkSlideOps(input: unknown): SlideOp[] {
  const ops = checkBatch(input, MAX_OPS, checkOp);
  const slides = ops.reduce((sum, op) => sum + (op.op === "slides" ? op.slides.length : 0), 0);
  if (slides > MAX_SLIDES) throw new ContentError(`One call builds at most ${MAX_SLIDES} slides; split the deck over several calls.`);
  // New slides have no ids yet: an order in the same call could not place them.
  if (ops.some((op) => op.op === "order") && ops.length > 1) throw new ContentError("order slides in a call of its own.");
  return ops;
}

function readScope(body: Raw): Scope {
  const view = body.view ?? "outline";
  if (view === "outline") return { action: "outline" };
  if (view === "slide") return { action: "slide", target: textArg(body.target, "target") };
  if (view === "search") return { action: "search", query: textArg(body.query, "query") };
  throw new ContentError('In a slide deck "view" is "outline", "slide" (target: a slide id) or "search".');
}

async function compile(raw: unknown, context: EditContext, measurer: Measurer): Promise<unknown[]> {
  const ops = checkSlideOps(raw);
  if (!ops.some((op) => op.op === "slides")) return ops;
  const size = (await context.run({ action: "size" })) as { width: number; height: number };
  const canvas: Canvas = { width: CANVAS_WIDTH, height: Math.round((CANVAS_WIDTH * size.height) / size.width), emuPerPx: size.width / CANVAS_WIDTH };
  const out: unknown[] = [];
  for (const [index, op] of ops.entries()) {
    if (op.op !== "slides") {
      out.push(op);
      continue;
    }
    const slides: unknown[] = [];
    for (const [n, { html, ...rest }] of op.slides.entries()) {
      try {
        slides.push({ ...rest, spec: await measurer.measure(html, canvas, context.page) });
      } catch (error) {
        if (error instanceof ContentError) throw new ContentError(`op ${index}, slide ${n}: ${error.message}`);
        // Not the slide's fault: a font the document server did not send, a page that crashed or ran out of time.
        const said = error instanceof Error ? error.message.split("\n")[0] : String(error);
        throw new EditorError(`op ${index}, slide ${n} could not be measured: ${said}`);
      }
    }
    out.push({ op: "slides", slides });
  }
  return out;
}

const SLIDE = readFileSync(new URL("../../editor/slide.js", import.meta.url), "utf8");

export function slideFormat(measurer: Measurer): Format {
  return {
    script: SLIDE,
    grouped: true,
    readScope,
    compile: (raw, context) => compile(raw, context, measurer),
    edit: async (context, ops) => (await context.run({ action: "edit", ops })) as Outcome,
  };
}
