/**
 * Builds the request atlas: the white-background PNG of the board plus
 * the geometry the model needs to answer in canvas coordinates.
 *
 * Ported from PenEcho's `src/client/app/ai-runtime.js` — `captureRectFor`
 * (553), `planViewportImage` (557), `buildViewportImage` (580) and
 * `mapHotspots` (523).
 *
 * The metadata keys here are a contract with the Python side: they are
 * read verbatim by `_whiteboard_note_from_metadata` in
 * `surogates/harness/loop_messages.py`. Renaming one silently shortens
 * the note the model sees rather than failing.
 */
import { type WbDoc, type WbObject, readingKey } from "./doc";
import { objectBounds, renderDoc } from "./render";
import type { RenderServices, View, ViewportSize } from "./render";

/** PenEcho's caps. Larger buys no accuracy and costs vision tokens. */
export const MAX_ATLAS_WIDTH = 2048;
export const MAX_ATLAS_HEIGHT = 1536;

/** The attention grid is 8x8, matching the prompt's description. */
export const HOTSPOT_GRID = 8;

/**
 * Minimum logical span of a capture, so a single small stroke still
 * arrives with enough surroundings for the model to place an answer.
 */
const MIN_CAPTURE_SPAN = 600;

/** Fraction of the capture span added as margin around the content. */
const CAPTURE_MARGIN = 0.25;

/**
 * Widest capture sent, in canvas units.
 *
 * Matched to the atlas caps so a capture at the limit still renders 1:1.
 * Past it the board would be squeezed to fit -- a 6800-unit board came
 * out at imageScale 0.30, where the model's own 55-unit answers are 16px
 * tall and it places new work on top of old by squinting.
 *
 * The board is infinite; the capture is not. What falls outside is
 * reported by {@link contentBeyond} rather than shrunk into
 * illegibility.
 */
const MAX_CAPTURE_SPAN_W = MAX_ATLAS_WIDTH;
const MAX_CAPTURE_SPAN_H = MAX_ATLAS_HEIGHT;

/**
 * Cells per side of the occupancy grid laid over the capture.
 *
 * Fixed, so the cost of describing a board is the same whether it holds
 * three objects or three hundred -- which is the whole point of sending
 * a grid instead of a list of rectangles.
 */
export const OCCUPANCY_GRID = 16;

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface AtlasPlan {
  sourceRect: Rect;
  imageScale: number;
  imageSize: { w: number; h: number };
  /** What the user can actually see, in logical units. */
  viewport: Rect;
}

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, v));

/** Two decimals is plenty for a cell size and keeps the note short. */
const round2 = (v: number) => Math.round(v * 100) / 100;

/** Union of every object's bounds, or null when the board is empty. */
export function contentBounds(
  doc: WbDoc,
  services: RenderServices,
): Rect | null {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const obj of doc.objects) {
    const b = objectBounds(obj, services);
    if (!b) continue;
    minX = Math.min(minX, b.x);
    minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.w);
    maxY = Math.max(maxY, b.y + b.h);
  }
  if (!Number.isFinite(minX)) return null;
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/**
 * Roughly how tall the user's handwriting is, in canvas units.
 *
 * The model is otherwise blind to scale: the note tells it where the
 * capture is and what it covers, but nothing about how big the strokes
 * in it are, so it picks a font size out of the air. On a real board of
 * ~250-unit digits it chose 90, and its answer landed a quarter the size
 * of the sum it was answering.
 *
 * Flat marks carry no height information -- the bars of an `=`, a minus,
 * the dot of an `i` -- and counting them drags the figure toward zero,
 * so strokes far shorter than typical are dropped. The cutoff is taken
 * from the median rather than the tallest: against the tallest, one
 * outsized stroke (a bracket, a long divider) excludes the very writing
 * being measured. Taking the median twice is what makes both the flat
 * marks and the outlier harmless.
 *
 * ponytail: fixed 25%-of-median cutoff, tuned on handwriting; revisit if
 * boards mixing two writing sizes read badly.
 */
export function inkHeight(doc: WbDoc, services: RenderServices): number | null {
  const heights: number[] = [];
  for (const obj of doc.objects) {
    if (obj.kind !== "ink") continue;
    const b = objectBounds(obj, services);
    if (b && b.h > 0) heights.push(b.h);
  }
  if (heights.length === 0) return null;
  const upright = heights.filter((h) => h >= median(heights) * 0.25);
  return upright.length === 0 ? null : median(upright);
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1
    ? sorted[mid]
    : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** The smallest rectangle covering both, or whichever one exists. */
function union(a: Rect | null, b: Rect | null): Rect | null {
  if (!a) return b;
  if (!b) return a;
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return {
    x,
    y,
    w: Math.max(a.x + a.w, b.x + b.w) - x,
    h: Math.max(a.y + a.h, b.y + b.h) - y,
  };
}

/** The viewport as a logical rectangle. */
function viewportRect(view: View, size: ViewportSize): Rect {
  return {
    x: view.x,
    y: view.y,
    w: size.w / view.zoom,
    h: size.h / view.zoom,
  };
}

/**
 * Choose the capture rectangle and its image scale.
 *
 * The capture is what the user is looking at, plus a margin of the board
 * around it, and always the latest input. Not the whole board: content
 * is unbounded and the atlas is not, so framing on everything squeezes a
 * long board into the same pixels until the model is placing objects by
 * squinting at a thumbnail. A 6800-unit board arrived at imageScale 0.30
 * -- 55-unit text rendered 16px tall -- and the model wrote its answer
 * on top of an earlier one. What falls outside is reported by
 * {@link contentBeyond} instead.
 *
 * Nor is it framed on the latest input alone. That is the *attention*
 * signal, carried by `latestInput` and the hotspot trail; framing on it
 * cropped away the very thing it referred to. A user who added `=` to a
 * finished sum got back two bare horizontal lines, which the model
 * reasonably read as a "menu / list icon?".
 *
 * Nothing is clamped to a canvas rectangle: the canvas has no edges.
 */
export function planAtlas(
  doc: WbDoc,
  latest: Rect | null,
  view: View,
  viewport: ViewportSize,
  services: RenderServices,
): AtlasPlan {
  const seen = viewportRect(view, viewport);
  // The margin is what lets an answer reference something just past the
  // edge of the screen, which is where the user's own eye already is.
  const around: Rect = {
    x: seen.x - seen.w * CAPTURE_MARGIN,
    y: seen.y - seen.h * CAPTURE_MARGIN,
    w: seen.w * (1 + CAPTURE_MARGIN * 2),
    h: seen.h * (1 + CAPTURE_MARGIN * 2),
  };

  // A board that fits is shown whole, wherever the user has scrolled to.
  // Framing on the viewport alone cuts expressions in half: a user
  // working at the right-hand end of an integral got back a capture
  // starting mid-`e^x`, with the integral sign off the left edge, and
  // the model answered the fragment it could see. Knowing that content
  // continues left is not the same as being able to read it.
  //
  // Only once the board outgrows a legible capture does the viewport
  // decide, because then something has to be left out and the least bad
  // thing to keep is what the user is looking at.
  const content = contentBounds(doc, services);
  const fitsWhole =
    content !== null &&
    content.w <= MAX_CAPTURE_SPAN_W &&
    content.h <= MAX_CAPTURE_SPAN_H;
  // Either way the latest input is in shot, even if the user scrolled
  // away after drawing it.
  const base = union(fitsWhole ? content : around, latest) as Rect;

  const cx = base.x + base.w / 2;
  const cy = base.y + base.h / 2;
  // Floored so a single tick mark on a blank board still arrives with
  // surroundings; capped so a long board still arrives legible.
  const spanW = clamp(base.w, MIN_CAPTURE_SPAN, MAX_CAPTURE_SPAN_W);
  const spanH = clamp(base.h, MIN_CAPTURE_SPAN, MAX_CAPTURE_SPAN_H);

  const sourceRect: Rect = {
    x: cx - spanW / 2,
    y: cy - spanH / 2,
    w: spanW,
    h: spanH,
  };

  // Math.min(1, ...) is what stops upscaling: a small capture is sent at
  // native size rather than blown up into wasted vision tokens.
  const imageScale = Math.min(
    1,
    MAX_ATLAS_WIDTH / sourceRect.w,
    MAX_ATLAS_HEIGHT / sourceRect.h,
  );
  return {
    sourceRect,
    imageScale,
    viewport: viewportRect(view, viewport),
    imageSize: {
      w: Math.max(
        1,
        Math.min(MAX_ATLAS_WIDTH, Math.ceil(sourceRect.w * imageScale)),
      ),
      h: Math.max(
        1,
        Math.min(MAX_ATLAS_HEIGHT, Math.ceil(sourceRect.h * imageScale)),
      ),
    },
  };
}

/**
 * Which cells of the capture already hold something.
 *
 * The model cannot be told to keep off existing work by looking: on a
 * busy board its own earlier answers are a few pixels tall, and the user
 * may have dragged, resized or deleted them since -- edits that exist
 * only in the document, never in the transcript. So the transcript is
 * not merely incomplete about the board, it is confidently out of date,
 * and this is the correction.
 *
 * A grid rather than a list of rectangles because the cost has to stay
 * flat: three objects and three hundred produce the same handful of
 * cells, where a rectangle list grows with the board until it dwarfs
 * everything else in the turn.
 *
 * Cells are returned as `[col, row]`, the same shape as
 * {@link mapHotspots}, so the model reads one spatial vocabulary rather
 * than two.
 */
export function occupancyCells(
  doc: WbDoc,
  sourceRect: Rect,
  services: RenderServices,
): number[][] {
  if (sourceRect.w <= 0 || sourceRect.h <= 0) return [];
  const seen = new Set<number>();
  const cells: number[][] = [];
  const cellW = sourceRect.w / OCCUPANCY_GRID;
  const cellH = sourceRect.h / OCCUPANCY_GRID;

  for (const obj of doc.objects) {
    if (obj.kind === "slot") continue;
    const b = objectBounds(obj, services);
    if (!b) continue;
    const c0 = Math.floor((b.x - sourceRect.x) / cellW);
    const c1 = Math.floor((b.x + b.w - sourceRect.x) / cellW);
    const r0 = Math.floor((b.y - sourceRect.y) / cellH);
    const r1 = Math.floor((b.y + b.h - sourceRect.y) / cellH);
    for (let r = Math.max(0, r0); r <= Math.min(OCCUPANCY_GRID - 1, r1); r++) {
      for (let c = Math.max(0, c0); c <= Math.min(OCCUPANCY_GRID - 1, c1); c++) {
        const key = r * OCCUPANCY_GRID + c;
        if (seen.has(key)) continue;
        seen.add(key);
        cells.push([c, r]);
      }
    }
  }
  // Reading order, so a run of free space is easy to spot.
  cells.sort((a, b) => a[1] - b[1] || a[0] - b[0]);
  return cells;
}

/** How many of the agent's own objects the turn note lists. */
export const AGENT_OBJECT_LIMIT = 8;

/** One of the agent's own objects, as the board holds it now. */
export interface AgentObjectReport {
  /** The `whiteboard_draw` call that produced it. */
  origin: string;
  label: string;
  /** Where it sits now, or null when the user has deleted it. */
  bounds: Rect | null;
  /** New user ink lands on or around it: an edit to it, not new work. */
  touched?: boolean;
}

/** A short, note-sized description of what an object is. */
function objectLabel(obj: WbObject): string {
  const raw =
    obj.kind === "text"
      ? obj.text
      : obj.kind === "formula"
        ? obj.latex
        : obj.kind === "artifact"
          ? `artifact ${obj.artifactId}`
          : obj.kind;
  const flat = String(raw).replace(/\s+/g, " ").trim();
  return flat.length > 40 ? `${flat.slice(0, 39)}…` : flat;
}

/** *rect* grown by *margin* on every side. */
function grow(rect: Rect, margin: number): Rect {
  return {
    x: rect.x - margin,
    y: rect.y - margin,
    w: rect.w + margin * 2,
    h: rect.h + margin * 2,
  };
}

function overlaps(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
  );
}

/**
 * What became of everything the agent has drawn.
 *
 * The transcript records where it *asked* for each object. It does not
 * record what happened next, because what happens next is the user:
 * dragging, resizing, deleting, or drawing something that changes what
 * an object means. None of that reaches the conversation, so the
 * agent's memory of its own work is confidently out of date.
 *
 * One real session ended with the agent's answer wrapped in
 * hand-drawn brackets and squared. The board said one thing, the
 * transcript another, and the agent answered the transcript.
 *
 * *newLocalIds* are the user's objects added since the last Ask; an
 * agent object they land on is flagged `touched`, because ink drawn
 * over or around an answer is an edit to it rather than a new question
 * beside it. The margin is generous on purpose: a bracket is drawn
 * just outside the thing it encloses, never across it.
 */
export function agentObjectReport(
  doc: WbDoc,
  services: RenderServices,
  opts: { newLocalIds?: ReadonlySet<string>; limit?: number } = {},
): AgentObjectReport[] {
  const limit = opts.limit ?? AGENT_OBJECT_LIMIT;
  const fresh: Rect[] = [];
  if (opts.newLocalIds?.size) {
    for (const obj of doc.objects) {
      if (obj.origin !== "local" || !opts.newLocalIds.has(obj.id)) continue;
      const b = objectBounds(obj, services);
      if (b) fresh.push(b);
    }
  }
  const reach = inkHeight(doc, services) ?? 0;

  // Newest first: the recent ones are the ones still being worked on,
  // and the cap has to fall on the oldest.
  const origins = doc.folded.slice(-limit).reverse();
  const report: AgentObjectReport[] = [];
  for (const origin of origins) {
    const mine = doc.objects.filter((o) => o.origin === origin);
    if (mine.length === 0) {
      report.push({ origin, label: "", bounds: null });
      continue;
    }
    for (const obj of mine) {
      const bounds = objectBounds(obj, services);
      const near = bounds ? grow(bounds, reach * 0.75) : null;
      report.push({
        origin,
        label: objectLabel(obj),
        bounds,
        touched: near ? fresh.some((f) => overlaps(near, f)) : false,
      });
    }
  }
  return report;
}

/** How many marks a turn carries. The note is permanent, so it is bounded. */
export const MAX_MARKS = 24;

/** A labelled, anchorable thing on the board. */
export interface BoardMark {
  /** `A3` for the user's ink, `B1` for the agent's objects, `S1` for a slot. */
  id: string;
  kind: "ink" | "agent" | "slot";
  /** Slot marks: the user's note on what belongs there. */
  hint?: string;
  /** Slot marks: the document object, so a fill can remove it. */
  objectId?: string;
  /** Null for an agent object the user has since removed. */
  rect: Rect | null;
  /** Agent marks: the `whiteboard_draw` call that produced it. */
  origin?: string;
  /** Agent marks: what it says, note-sized. */
  label?: string;
  /** Ink marks: holds ink drawn since the last Ask. */
  fresh?: boolean;
  /** Agent marks: new ink landed on or around it. */
  touched?: boolean;
  /** Ink marks: the strokes in the cluster, the key its reading lives under. */
  strokes?: string[];
  /** Ink marks: what it says, if it has been read. */
  reading?: string;
  readBy?: "agent" | "user";
}

/** One cluster of the user's strokes and the strokes in it. */
export interface InkCluster {
  rect: Rect;
  strokeIds: string[];
}

/**
 * Group the user's strokes into the things they wrote.
 *
 * Two strokes belong together when their boxes, grown by a margin
 * scaled to the handwriting, overlap: wide sideways (the gaps between
 * symbols in one expression) and narrow vertically (the gap between
 * two lines is what separates them). Single-linkage over that relation
 * is what a person means by "that expression".
 *
 * Reading order: top to bottom in bands one line high, then left to
 * right, so `A1` is where a reader starts.
 *
 * ponytail: O(n²) pairwise pass; a grid index if a board ever holds
 * thousands of strokes.
 */
export function inkClusters(
  doc: WbDoc,
  services: RenderServices,
  unit: number,
): InkCluster[] {
  const strokes: { id: string; box: Rect; grown: Rect }[] = [];
  for (const obj of doc.objects) {
    if (obj.kind !== "ink" || obj.origin !== "local") continue;
    const box = objectBounds(obj, services);
    if (!box) continue;
    strokes.push({
      id: obj.id,
      box,
      grown: {
        x: box.x - unit * 0.8,
        y: box.y - unit * 0.4,
        w: box.w + unit * 1.6,
        h: box.h + unit * 0.8,
      },
    });
  }
  const parent = strokes.map((_, i) => i);
  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]];
      i = parent[i];
    }
    return i;
  };
  for (let i = 0; i < strokes.length; i++) {
    for (let j = i + 1; j < strokes.length; j++) {
      if (overlaps(strokes[i].grown, strokes[j].grown)) {
        parent[find(i)] = find(j);
      }
    }
  }
  const groups = new Map<number, InkCluster>();
  strokes.forEach((s, i) => {
    const root = find(i);
    const g = groups.get(root);
    if (!g) {
      groups.set(root, { rect: { ...s.box }, strokeIds: [s.id] });
      return;
    }
    const x = Math.min(g.rect.x, s.box.x);
    const y = Math.min(g.rect.y, s.box.y);
    g.rect = {
      x,
      y,
      w: Math.max(g.rect.x + g.rect.w, s.box.x + s.box.w) - x,
      h: Math.max(g.rect.y + g.rect.h, s.box.y + s.box.h) - y,
    };
    g.strokeIds.push(s.id);
  });
  const band = Math.max(unit * 1.5, 1);
  return [...groups.values()].sort(
    (a, b) =>
      Math.round(a.rect.y / band) - Math.round(b.rect.y / band) ||
      a.rect.x - b.rect.x,
  );
}

/**
 * Everything the model can point at, labelled.
 *
 * The user's ink as clusters (`A1`, `A2`, …) and the agent's own objects
 * (`B1`, `B2`, …). The same labels are drawn on the atlas, listed in the
 * turn note, and accepted as `anchor`/`replaces`, so "the answer goes
 * right of A3" is one name shared by the picture, the text and the tool.
 */
export function boardMarks(
  doc: WbDoc,
  services: RenderServices,
  opts: { newLocalIds?: ReadonlySet<string>; unit: number },
): BoardMark[] {
  const fresh = opts.newLocalIds ?? new Set<string>();
  let clusters = inkClusters(doc, services, opts.unit).map((c) => ({
    ...c,
    fresh: c.strokeIds.some((id) => fresh.has(id)),
  }));
  if (clusters.length > MAX_MARKS) {
    // Keep what is new, then the rest in reading order, then restore
    // reading order for labelling.
    const keep = new Set(
      [...clusters]
        .sort((a, b) => Number(b.fresh) - Number(a.fresh))
        .slice(0, MAX_MARKS),
    );
    clusters = clusters.filter((c) => keep.has(c));
  }
  const readings = doc.readings ?? {};
  const marks: BoardMark[] = clusters.map((c, i) => {
    const read = readings[readingKey(c.strokeIds)];
    return {
      id: `A${i + 1}`,
      kind: "ink",
      rect: c.rect,
      strokes: c.strokeIds,
      ...(c.fresh ? { fresh: true } : {}),
      ...(read ? { reading: read.text, readBy: read.source } : {}),
    };
  });
  agentObjectReport(doc, services, { newLocalIds: opts.newLocalIds }).forEach(
    (o, i) => {
      marks.push({
        id: `B${i + 1}`,
        kind: "agent",
        rect: o.bounds,
        origin: o.origin,
        label: o.label,
        ...(o.touched ? { touched: true } : {}),
      });
    },
  );
  // Slots: the space the user reserved for the answer. Listed last so
  // the ids stay stable while marks above them come and go.
  doc.objects
    .filter((o): o is Extract<WbObject, { kind: "slot" }> => o.kind === "slot")
    .forEach((o, i) => {
      marks.push({
        id: `S${i + 1}`,
        kind: "slot",
        rect: { x: o.x, y: o.y, w: o.w, h: o.h },
        objectId: o.id,
        ...(o.hint ? { hint: o.hint } : {}),
      });
    });
  return marks;
}

/**
 * Draw the marks: a box around each thing and its label at the corner.
 *
 * Amber, so it cannot be confused with the blue grid or with ink. `map`
 * takes a board rect to the space the context is in — image pixels for
 * the atlas, board units for the live canvas — and `fontPx` is the
 * label size in that space.
 */
export function paintMarks(
  ctx: CanvasRenderingContext2D,
  marks: BoardMark[],
  map: (r: Rect) => Rect,
  fontPx: number,
  lineWidth: number,
  opts: { showReadings?: boolean } = {},
): void {
  ctx.save();
  ctx.lineWidth = lineWidth;
  ctx.font = `bold ${fontPx}px system-ui, sans-serif`;
  ctx.textBaseline = "top";
  for (const mark of marks) {
    if (!mark.rect) continue;
    const r = map(mark.rect);
    if (mark.kind !== "slot") {
      // A slot paints its own dashed box with the document.
      ctx.strokeStyle = mark.fresh
        ? "rgba(217, 119, 6, 0.9)"
        : "rgba(217, 119, 6, 0.5)";
      ctx.strokeRect(r.x, r.y, r.w, r.h);
    }
    const padX = fontPx * 0.35;
    const tagW = ctx.measureText(mark.id).width + padX * 2;
    const tagH = fontPx * 1.3;
    ctx.fillStyle = "rgba(217, 119, 6, 0.95)";
    ctx.fillRect(r.x, r.y - tagH, tagW, tagH);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(mark.id, r.x + padX, r.y - tagH + fontPx * 0.15);
    // The stored reading, under the ink, so the user can see what the
    // board believes it says and fix it. Live canvas only: the note
    // carries it to the model, and on the picture it would be clutter.
    if (opts.showReadings && mark.reading) {
      ctx.font = `italic ${fontPx}px system-ui, sans-serif`;
      ctx.fillStyle =
        mark.readBy === "user" ? "rgba(22, 101, 52, 0.85)" : "rgba(107, 114, 128, 0.85)";
      ctx.fillText(mark.reading, r.x, r.y + r.h + fontPx * 0.3);
      ctx.font = `bold ${fontPx}px system-ui, sans-serif`;
    }
  }
  ctx.restore();
}

/** Target height of the handwriting in a close-up, in pixels. */
const CROP_INK_PX = 110;
/** Longest side a close-up may have. */
const CROP_MAX_PX = 1536;
/** Close-ups per turn. Readings persist, so it is normally one. */
export const MAX_CROPS = 2;

/** One close-up: the marks it shows and the board rect it covers. */
export interface CropRegion {
  ids: string[];
  rect: Rect;
}

export interface InkCrop {
  ids: string[];
  canvas: HTMLCanvasElement;
  scale: number;
}

function unionRect(a: Rect, b: Rect): Rect {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return {
    x,
    y,
    w: Math.max(a.x + a.w, b.x + b.w) - x,
    h: Math.max(a.y + a.h, b.y + b.h) - y,
  };
}

/**
 * What to show close up this turn: the new, unread ink -- together with
 * whatever it was written on.
 *
 * A crop of the new ink alone is the wrong picture when the ink is an
 * operation on something already there. A user wrapping the agent's
 * answer in `( )²` produces a new mark holding just `)²`, which read
 * alone is a `?`; the meaning is only visible with the answer inside
 * the brackets. So a fresh mark pulls in the agent objects it touches
 * and any other fresh marks near it, and the region is cropped whole.
 */
export function cropRegions(marks: BoardMark[], unit: number): CropRegion[] {
  const fresh = marks.filter(
    (m) => m.kind === "ink" && m.rect && m.fresh && !m.reading,
  );
  if (fresh.length === 0) return [];
  const touched = marks.filter((m) => m.kind === "agent" && m.rect && m.touched);
  const reach = Math.max(unit, 8);

  let regions: CropRegion[] = fresh.map((m) => ({
    ids: [m.id],
    rect: m.rect as Rect,
  }));
  for (const agent of touched) {
    const near = regions.find((r) => overlaps(grow(r.rect, reach), agent.rect as Rect));
    if (near) {
      near.ids.push(agent.id);
      near.rect = unionRect(near.rect, agent.rect as Rect);
    }
  }
  // Merge regions that now overlap each other, until none do.
  let merged = true;
  while (merged) {
    merged = false;
    for (let i = 0; i < regions.length && !merged; i++) {
      for (let j = i + 1; j < regions.length; j++) {
        if (overlaps(grow(regions[i].rect, reach), regions[j].rect)) {
          regions[i] = {
            ids: [...regions[i].ids, ...regions[j].ids],
            rect: unionRect(regions[i].rect, regions[j].rect),
          };
          regions.splice(j, 1);
          merged = true;
          break;
        }
      }
    }
  }
  regions = regions.slice(0, MAX_CROPS);
  for (const r of regions) {
    r.ids.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }
  return regions;
}

/**
 * Render one region large enough to read.
 *
 * Every misread so far came from glyphs a few dozen pixels tall on the
 * overview: `1)` and `√(` are hard to tell apart at that size. The
 * overview still carries context and placement; this carries
 * legibility. Scaled so the handwriting is about CROP_INK_PX tall,
 * never below 1:1 unless the region is too wide for the cap, and
 * framed with a margin of one line.
 */
export function buildRegionCrop(
  doc: WbDoc,
  region: CropRegion,
  services: RenderServices,
  unit: number,
): InkCrop | null {
  const margin = Math.max(unit * 0.6, 8);
  const area = grow(region.rect, margin);
  // A close-up that is sent must magnify: at 118px handwriting the
  // target height alone gave scale 1, and the model got the overview
  // twice. Floor at 1.5x; the size cap still wins for a huge region.
  let scale = Math.max(1.5, CROP_INK_PX / Math.max(unit, 1));
  scale = Math.min(scale, CROP_MAX_PX / area.w, CROP_MAX_PX / area.h);
  scale = Math.max(scale, 0.25);
  const size = {
    w: Math.max(1, Math.ceil(area.w * scale)),
    h: Math.max(1, Math.ceil(area.h * scale)),
  };
  const canvas = services.createCanvas(size.w, size.h);
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, size.w, size.h);
  renderDoc(
    ctx,
    { ...doc, objects: doc.objects.map((o) => ({ ...o, selected: false })) },
    { x: area.x, y: area.y, zoom: scale },
    size,
    services,
  );
  return { ids: region.ids, canvas, scale: Math.round(scale * 100) / 100 };
}

/**
 * Which way the board continues past the capture.
 *
 * The capture is bounded, so its edge is not the edge of anything. Say
 * so, or the model treats the empty margin as free canvas and places
 * work into objects sitting just outside the frame.
 */
export function contentBeyond(
  doc: WbDoc,
  sourceRect: Rect,
  services: RenderServices,
): string[] {
  const content = contentBounds(doc, services);
  if (!content) return [];
  const out: string[] = [];
  if (content.y < sourceRect.y) out.push("above");
  if (content.x + content.w > sourceRect.x + sourceRect.w) out.push("right");
  if (content.y + content.h > sourceRect.y + sourceRect.h) out.push("below");
  if (content.x < sourceRect.x) out.push("left");
  return out;
}

/**
 * Project pen positions onto an 8x8 grid over *sourceRect*.
 *
 * Points outside the rectangle are dropped and consecutive duplicates
 * collapsed, but a revisited cell that is not consecutive is kept: the
 * trajectory is the point, and doubling back is real information about
 * reading order.
 */
export function mapHotspots(
  sourceRect: Rect,
  points: { x: number; y: number }[],
): number[][] {
  if (sourceRect.w <= 0 || sourceRect.h <= 0) return [];
  const cells: number[][] = [];
  for (const p of points) {
    if (
      p.x < sourceRect.x ||
      p.x > sourceRect.x + sourceRect.w ||
      p.y < sourceRect.y ||
      p.y > sourceRect.y + sourceRect.h
    ) {
      continue;
    }
    // Clamp: a point exactly on the far edge divides to HOTSPOT_GRID,
    // one past the last index.
    const col = clamp(
      Math.floor(((p.x - sourceRect.x) / sourceRect.w) * HOTSPOT_GRID),
      0,
      HOTSPOT_GRID - 1,
    );
    const row = clamp(
      Math.floor(((p.y - sourceRect.y) / sourceRect.h) * HOTSPOT_GRID),
      0,
      HOTSPOT_GRID - 1,
    );
    const last = cells[cells.length - 1];
    if (last && last[0] === col && last[1] === row) continue;
    cells.push([col, row]);
  }
  return cells;
}

/**
 * Render the board into a white-background canvas at the planned scale.
 *
 * White, not transparent: the prompt tells the model the image is a
 * clean white-background rendering, and a transparent PNG composites as
 * black on some providers.
 */
export function buildAtlas(
  doc: WbDoc,
  plan: AtlasPlan,
  services: RenderServices,
  marks: BoardMark[] = [],
): HTMLCanvasElement {
  const canvas = services.createCanvas(plan.imageSize.w, plan.imageSize.h);
  const ctx = canvas.getContext("2d");
  if (!ctx) return canvas;

  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, plan.imageSize.w, plan.imageSize.h);

  renderDoc(
    ctx,
    // Selection chrome is interface, not content: the model must not see
    // a dashed box and mistake it for something the user drew.
    { ...doc, objects: doc.objects.map((o) => ({ ...o, selected: false })) },
    { x: plan.sourceRect.x, y: plan.sourceRect.y, zoom: plan.imageScale },
    plan.imageSize,
    services,
  );
  paintGridOverlay(ctx, plan.imageSize);
  // Marks go on last so their labels sit above everything else.
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  paintMarks(
    ctx,
    marks,
    (r) => ({
      x: (r.x - plan.sourceRect.x) * plan.imageScale,
      y: (r.y - plan.sourceRect.y) * plan.imageScale,
      w: r.w * plan.imageScale,
      h: r.h * plan.imageScale,
    }),
    13,
    1.5,
  );
  return canvas;
}

/**
 * Draw the occupancy grid over the finished atlas, labelled.
 *
 * The cells are already described in the note as `[col, row]` pairs, but
 * reading those means cross-referencing a list against a picture and
 * doing arithmetic on `sourceRect` to place anything. Drawn on, the model
 * can see which cells hold work and which are free, and name a
 * destination by pointing rather than by calculating.
 *
 * Deliberately faint and cool-toned: it has to be legible enough to
 * count but never mistakable for something the user drew. The note says
 * so as well -- both, because either alone has been enough for a model
 * to answer the scaffolding instead of the question.
 *
 * Image space only. `renderDoc` leaves a board-space transform behind,
 * and the grid is a property of the picture, not of the canvas.
 */
function paintGridOverlay(
  ctx: CanvasRenderingContext2D,
  size: { w: number; h: number },
): void {
  const cellW = size.w / OCCUPANCY_GRID;
  const cellH = size.h / OCCUPANCY_GRID;

  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.strokeStyle = "rgba(37, 99, 235, 0.18)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < OCCUPANCY_GRID; i++) {
    // The half-pixel keeps a 1px line on the pixel rather than across
    // two, which otherwise renders as a 2px smear.
    const x = Math.round(i * cellW) + 0.5;
    const y = Math.round(i * cellH) + 0.5;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, size.h);
    ctx.moveTo(0, y);
    ctx.lineTo(size.w, y);
  }
  ctx.stroke();

  // Indices along the top and left edges only. One label per cell would
  // put 256 numbers over the board.
  ctx.fillStyle = "rgba(37, 99, 235, 0.55)";
  ctx.font = "11px system-ui, sans-serif";
  ctx.textBaseline = "top";
  for (let i = 0; i < OCCUPANCY_GRID; i++) {
    ctx.fillText(String(i), i * cellW + 3, 2);
    if (i > 0) ctx.fillText(String(i), 3, i * cellH + 2);
  }
  ctx.restore();
}

export interface AtlasExtras {
  mode?: "sketch" | "deep";
  selection?: Rect | null;
  typedInput?: string;
  /** See {@link inkHeight}. Omitted when the board carries no ink. */
  inkHeight?: number | null;
  /** See {@link occupancyCells}. */
  occupied?: number[][];
  /** See {@link contentBeyond}. */
  beyond?: string[];
  /** See {@link agentObjectReport}. Superseded by `marks`. */
  agentObjects?: AgentObjectReport[];
  /** See {@link boardMarks}. */
  marks?: BoardMark[];
  /** Close-ups attached after the overview: which marks, which image. */
  crops?: { marks: string[]; imageIndex: number; scale: number }[];
  /** Which action button sent the turn; absent only for API callers. */
  action?: UserAction;
}

/**
 * The user's own answer to "what do you want": PenEcho's action menu,
 * as the board's send buttons. `hint` is the tooltip.
 */
export type UserAction = "answer" | "continue" | "explain" | "hint";
export const USER_ACTIONS: { id: UserAction; label: string; hint: string }[] = [
  {
    id: "answer",
    label: "Answer",
    hint: "Solve or complete what is on the board and write the result: into the answer box if you placed one, otherwise next to your ink.",
  },
  {
    id: "continue",
    label: "Continue",
    hint: "Pick up where your drawing or text stops and extend it in the same style.",
  },
  {
    id: "explain",
    label: "Explain",
    hint: "Explain what is on the board in words, without changing it.",
  },
  {
    id: "hint",
    label: "Hint",
    hint: "Give a clue toward the answer, never the answer itself.",
  },
];

/**
 * The `metadata.whiteboard` payload for one turn.
 *
 * Every key here is read by name on the Python side; optional ones are
 * omitted rather than sent null, because the note builder tests for
 * presence.
 */
export function atlasMetadata(
  plan: AtlasPlan,
  latest: Rect | null,
  hotspots: number[][],
  extras: AtlasExtras,
): Record<string, unknown> {
  const meta: Record<string, unknown> = {
    sourceRect: plan.sourceRect,
    imageScale: plan.imageScale,
    viewport: plan.viewport,
    infinite: true,
    mode: extras.mode === "deep" ? "deep" : "sketch",
  };
  if (latest) meta.latestInput = latest;
  if (hotspots.length > 0) meta.hotspots = hotspots;
  if (extras.selection) meta.selection = extras.selection;
  if (extras.typedInput?.trim()) meta.typedInput = extras.typedInput.trim();
  if (extras.inkHeight && extras.inkHeight > 0) {
    meta.inkHeight = Math.round(extras.inkHeight);
  }
  if (extras.occupied?.length) {
    meta.occupied = extras.occupied;
    meta.occupancyGrid = OCCUPANCY_GRID;
    // So a chosen cell converts to canvas coordinates without inverting
    // the image formula by hand. Asked for the slot after an `x =`, the
    // model converted the column correctly and left the row in image
    // coordinates, landing the answer below the frame it was shown.
    meta.cellSize = {
      w: round2(plan.sourceRect.w / OCCUPANCY_GRID),
      h: round2(plan.sourceRect.h / OCCUPANCY_GRID),
    };
  }
  if (extras.beyond?.length) meta.beyond = extras.beyond;
  if (extras.marks?.length) {
    meta.marks = extras.marks.map((m) => ({
      id: m.id,
      kind: m.kind,
      ...(m.rect
        ? {
            x: round2(m.rect.x),
            y: round2(m.rect.y),
            w: round2(m.rect.w),
            h: round2(m.rect.h),
          }
        : { removed: true }),
      ...(m.origin ? { origin: m.origin } : {}),
      ...(m.label ? { label: m.label } : {}),
      ...(m.fresh ? { fresh: true } : {}),
      ...(m.touched ? { touched: true } : {}),
      ...(m.strokes ? { strokes: m.strokes } : {}),
      ...(m.reading ? { reading: m.reading, readBy: m.readBy } : {}),
      ...(m.hint ? { hint: m.hint } : {}),
      ...(m.objectId ? { objectId: m.objectId } : {}),
    }));
  }
  if (extras.crops?.length) meta.crops = extras.crops;
  if (extras.action) meta.action = extras.action;
  if (extras.agentObjects?.length) {
    meta.agentObjects = extras.agentObjects.map((o) => ({
      origin: o.origin,
      label: o.label,
      ...(o.bounds
        ? {
            x: round2(o.bounds.x),
            y: round2(o.bounds.y),
            w: round2(o.bounds.w),
            h: round2(o.bounds.h),
          }
        : { removed: true }),
      ...(o.touched ? { touched: true } : {}),
    }));
  }
  return meta;
}
