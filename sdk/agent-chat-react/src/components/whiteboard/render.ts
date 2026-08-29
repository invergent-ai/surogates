import {
  type DrawBounds,
  normalize as normalizeDraw,
  render as renderDraw,
} from "./draw";
import { COORD_LIMIT, type WbDoc, type WbObject } from "./doc";
import type { RasterFormula } from "./formula";

export interface View {
  /** Logical coordinate at the viewport's top-left corner. */
  x: number;
  y: number;
  zoom: number;
}

export interface ViewportSize {
  w: number;
  h: number;
}

/**
 * The measurement and rasterisation services the renderer needs but does
 * not own. Passed in rather than imported so the render path stays a
 * pure function of its inputs and can be exercised without a real canvas.
 */
export interface RenderServices {
  /** Size a formula without waiting for its glyphs. */
  formula(latex: string, fontSize: number): { w: number; h: number };
  /** The rasterised formula, or null while it is still decoding. */
  formulaImage(latex: string, fontSize: number): RasterFormula | null;
  createCanvas(w: number, h: number): HTMLCanvasElement;
}

/** Average glyph advance as a fraction of font size, for estimating
 *  text extents without a context. Matches `layout.ts`, the atlas and
 *  the server-side wrap check. */
const TEXT_GLYPH_ADVANCE = 0.6;

/** Ink colour applied to agent-authored geometry. */
const AGENT_COLOR = "#2563eb";
const TEXT_COLOR = "#111827";
const SELECTION_COLOR = "#2563eb";
const ARTIFACT_FRAME_COLOR = "#94a3b8";

const SELECTION_PAD = 6;
export const HANDLE_SIZE = 8;

/**
 * Which resize handle, if any, sits under *pt*.
 *
 * Screen-space slop scaled back into logical units, so the grab target
 * stays a constant size on screen however far you have zoomed out —
 * otherwise the handles become unhittable at low zoom.
 */
export function handleAt(
  bounds: DrawBounds,
  pt: { x: number; y: number },
  zoom: number,
): "nw" | "ne" | "sw" | "se" | null {
  const r = (HANDLE_SIZE / zoom) * 1.5;
  const corners = [
    ["nw", bounds.x, bounds.y],
    ["ne", bounds.x + bounds.w, bounds.y],
    ["sw", bounds.x, bounds.y + bounds.h],
    ["se", bounds.x + bounds.w, bounds.y + bounds.h],
  ] as const;
  for (const [name, cx, cy] of corners) {
    if (Math.abs(pt.x - cx) <= r && Math.abs(pt.y - cy) <= r) return name;
  }
  return null;
}

/** The corner opposite *handle*, which stays put during a resize. */
export function oppositeCorner(
  bounds: DrawBounds,
  handle: "nw" | "ne" | "sw" | "se",
): { x: number; y: number } {
  return {
    x: handle === "nw" || handle === "sw" ? bounds.x + bounds.w : bounds.x,
    y: handle === "nw" || handle === "ne" ? bounds.y + bounds.h : bounds.y,
  };
}

/** Whether two rectangles overlap at all. */
function intersects(a: DrawBounds, b: DrawBounds): boolean {
  return (
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
  );
}

/**
 * Ids of every object touching *rect*.
 *
 * Intersection rather than containment: a marquee that only selects
 * fully-enclosed objects misses the long stroke you dragged across,
 * which is usually the thing you meant.
 */
export function objectsInRect(
  doc: WbDoc,
  rect: DrawBounds,
  services: RenderServices,
): string[] {
  const ids: string[] = [];
  for (const obj of doc.objects) {
    const b = objectBounds(obj, services);
    if (b && intersects(b, rect)) ids.push(obj.id);
  }
  return ids;
}

/** Normalise a drag between two corners into a positive-extent rect. */
export function rectFromCorners(
  a: { x: number; y: number },
  b: { x: number; y: number },
): DrawBounds {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    w: Math.abs(b.x - a.x),
    h: Math.abs(b.y - a.y),
  };
}

/** Union of every selected object's bounds, or null. */
export function selectionBounds(
  doc: WbDoc,
  services: RenderServices,
): DrawBounds | null {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const obj of doc.objects) {
    if (!obj.selected) continue;
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

// ---------------------------------------------------------------------
// Bounds
// ---------------------------------------------------------------------

function inkBounds(pts: number[], width: number): DrawBounds | null {
  if (pts.length < 2) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i + 1 < pts.length; i += 2) {
    minX = Math.min(minX, pts[i]);
    maxX = Math.max(maxX, pts[i]);
    minY = Math.min(minY, pts[i + 1]);
    maxY = Math.max(maxY, pts[i + 1]);
  }
  // Half the stroke sits either side of the path's centreline, so a bare
  // point extent under-reports a thick stroke and clips its selection box.
  const pad = width / 2;
  return {
    x: minX - pad,
    y: minY - pad,
    w: maxX - minX + width,
    h: maxY - minY + width,
  };
}

function drawCommandOf(obj: Extract<WbObject, { kind: "draw" }>) {
  return {
    origin: obj.origin_,
    types: obj.types,
    items: obj.items,
    width: obj.width,
    tension: obj.tension,
    closed: obj.closed,
    fill: obj.fill,
    arrows: obj.arrows,
  };
}

/**
 * The object's logical bounding box, or `null` when it has none.
 *
 * `erase` is the only `null` case: it is a clipping instruction rather
 * than a thing on the board, so it must never be selected or hit-tested.
 */
export function objectBounds(
  obj: WbObject,
  services: RenderServices,
): DrawBounds | null {
  switch (obj.kind) {
    case "ink":
      return inkBounds(obj.pts, obj.width);
    case "draw": {
      // The draw normalizer already accounts for curve extrema,
      // stroke padding and arrowheads — exactly the computation not
      // worth rewriting.
      const normalized = normalizeDraw(drawCommandOf(obj), COORD_LIMIT);
      return normalized ? normalized._draw.bounds : null;
    }
    case "text": {
      // Estimated from the glyph count with the same average advance
      // every other estimate in the board uses. It used to report one
      // line whatever the text: a two-line answer then had a one-line
      // box, the collision nudge cleared only its first line, and the
      // next answer was placed straight onto its second. Nothing here
      // has a canvas context to measure with, and being a little wrong
      // about a line's width is far cheaper than being a whole line
      // wrong about its height.
      const step = Math.max(obj.fontSize * obj.lineHeight, obj.fontSize);
      let lines = 0;
      let widest = 0;
      for (const paragraph of obj.text.split("\n")) {
        const width = paragraph.length * obj.fontSize * TEXT_GLYPH_ADVANCE;
        lines += Math.max(1, Math.ceil(width / obj.maxWidth));
        widest = Math.max(widest, Math.min(width, obj.maxWidth));
      }
      return {
        x: obj.x,
        y: obj.y,
        w: Math.max(widest, obj.fontSize),
        h: Math.max(1, lines) * step,
      };
    }
    case "formula": {
      const { w, h } = services.formula(obj.latex, obj.fontSize);
      return { x: obj.x, y: obj.y, w, h };
    }
    case "artifact":
    case "slot":
      return { x: obj.x, y: obj.y, w: obj.w, h: obj.h };
    case "erase":
      return null;
  }
}

function contains(bounds: DrawBounds, pt: { x: number; y: number }): boolean {
  return (
    pt.x >= bounds.x &&
    pt.x <= bounds.x + bounds.w &&
    pt.y >= bounds.y &&
    pt.y <= bounds.y + bounds.h
  );
}

/**
 * The topmost object under *pt*, or `null`.
 *
 * Walks backwards because array position is z-order: the last object
 * painted is the one the user sees on top and therefore means to grab.
 */
export function hitTest(
  doc: WbDoc,
  pt: { x: number; y: number },
  services: RenderServices,
): WbObject | null {
  for (let i = doc.objects.length - 1; i >= 0; i--) {
    const obj = doc.objects[i];
    const bounds = objectBounds(obj, services);
    if (bounds && contains(bounds, pt)) return obj;
  }
  return null;
}

// ---------------------------------------------------------------------
// Painting
// ---------------------------------------------------------------------

/** Split *text* into lines that fit *maxWidth* at the current font. */
function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string[] {
  const lines: string[] = [];
  for (const paragraph of text.split("\n")) {
    let line = "";
    for (const word of paragraph.split(/\s+/)) {
      if (!word) continue;
      const candidate = line ? `${line} ${word}` : word;
      if (line && ctx.measureText(candidate).width > maxWidth) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    lines.push(line);
  }
  return lines;
}

function paintInk(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "ink" }>,
): void {
  if (obj.pts.length < 4) return;
  ctx.beginPath();
  ctx.moveTo(obj.pts[0], obj.pts[1]);
  for (let i = 2; i + 1 < obj.pts.length; i += 2) {
    ctx.lineTo(obj.pts[i], obj.pts[i + 1]);
  }
  ctx.lineWidth = obj.width;
  ctx.strokeStyle = obj.color;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();
}

function paintText(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "text" }>,
): void {
  ctx.font = `${obj.fontSize}px system-ui, sans-serif`;
  ctx.fillStyle = TEXT_COLOR;
  ctx.textBaseline = "top";
  const step = obj.fontSize * obj.lineHeight;
  wrapText(ctx, obj.text, obj.maxWidth).forEach((line, i) => {
    ctx.fillText(line, obj.x, obj.y + i * step);
  });
}

function paintDraw(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "draw" }>,
  services: RenderServices,
): void {
  const rendered = renderDraw(
    drawCommandOf(obj),
    services.createCanvas,
    AGENT_COLOR,
  );
  if (!rendered) return;
  ctx.drawImage(rendered.image, rendered.x, rendered.y);
}

function paintFormula(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "formula" }>,
  services: RenderServices,
): void {
  const raster = services.formulaImage(obj.latex, obj.fontSize);
  if (raster) {
    ctx.drawImage(raster.image, obj.x, obj.y, raster.w, raster.h);
    return;
  }
  // Still decoding: show the source rather than a gap, so the board
  // never looks like the command was dropped.
  const { h } = services.formula(obj.latex, obj.fontSize);
  ctx.font = `${obj.fontSize * 0.7}px ui-monospace, monospace`;
  ctx.fillStyle = ARTIFACT_FRAME_COLOR;
  ctx.textBaseline = "top";
  ctx.fillText(obj.latex, obj.x, obj.y + h * 0.25);
}

function paintArtifactFrame(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "artifact" }>,
): void {
  // The artifact itself renders as a positioned DOM overlay: a canvas
  // cannot host the iframe an `html` artifact needs. The frame is what
  // the atlas carries, so the model can see that the space is occupied
  // and place its answer somewhere else.
  ctx.save();
  ctx.setLineDash([6, 4]);
  ctx.lineWidth = 1;
  ctx.strokeStyle = ARTIFACT_FRAME_COLOR;
  ctx.strokeRect(obj.x, obj.y, obj.w, obj.h);
  ctx.restore();
}

function paintSlot(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "slot" }>,
): void {
  // Reserved space: a dashed amber box the model sees on the atlas
  // exactly as the user does. The label tag is painted with the marks.
  ctx.save();
  ctx.setLineDash([8, 6]);
  ctx.lineWidth = 2;
  ctx.strokeStyle = "rgba(217, 119, 6, 0.85)";
  ctx.fillStyle = "rgba(217, 119, 6, 0.06)";
  ctx.fillRect(obj.x, obj.y, obj.w, obj.h);
  ctx.strokeRect(obj.x, obj.y, obj.w, obj.h);
  // The robot says what the box is for, on the canvas and on the atlas
  // alike: the answer goes here.
  const glyph = Math.max(12, Math.min(obj.h * 0.5, obj.w * 0.3, 40));
  ctx.font = `${glyph}px system-ui, sans-serif`;
  ctx.textBaseline = "top";
  ctx.fillStyle = "rgba(180, 83, 9, 0.9)";
  ctx.fillText("\u{1F916}", obj.x + glyph * 0.3, obj.y + (obj.h - glyph) / 2);
  if (obj.hint) {
    const size = Math.max(10, Math.min(obj.h * 0.3, (obj.w - glyph * 1.6) / Math.max(obj.hint.length, 1) / 0.6, 22));
    ctx.font = `italic ${size}px system-ui, sans-serif`;
    ctx.fillStyle = "rgba(180, 83, 9, 0.8)";
    ctx.fillText(obj.hint, obj.x + glyph * 1.6, obj.y + (obj.h - size) / 2);
  }
  ctx.restore();
}

function paintErase(
  ctx: CanvasRenderingContext2D,
  obj: Extract<WbObject, { kind: "erase" }>,
): void {
  ctx.save();
  ctx.globalCompositeOperation = "destination-out";
  if (obj.mode === "rect") {
    ctx.fillRect(obj.x ?? 0, obj.y ?? 0, obj.w ?? 0, obj.h ?? 0);
  } else if (obj.points && obj.points.length > 1) {
    ctx.beginPath();
    ctx.moveTo(obj.points[0][0], obj.points[0][1]);
    for (const [x, y] of obj.points.slice(1)) ctx.lineTo(x, y);
    ctx.lineWidth = obj.size ?? 20;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
  }
  ctx.restore();
}

function paintSelection(
  ctx: CanvasRenderingContext2D,
  bounds: DrawBounds,
  zoom: number,
): void {
  ctx.save();
  // Chrome is drawn under the view transform, so undo the zoom on line
  // widths and handles — otherwise the outline thickens as you zoom in.
  const px = 1 / zoom;
  ctx.setLineDash([4 * px, 3 * px]);
  ctx.lineWidth = px;
  ctx.strokeStyle = SELECTION_COLOR;
  ctx.strokeRect(
    bounds.x - SELECTION_PAD * px,
    bounds.y - SELECTION_PAD * px,
    bounds.w + SELECTION_PAD * 2 * px,
    bounds.h + SELECTION_PAD * 2 * px,
  );
  ctx.setLineDash([]);
  ctx.fillStyle = SELECTION_COLOR;
  const s = HANDLE_SIZE * px;
  for (const [cx, cy] of [
    [bounds.x, bounds.y],
    [bounds.x + bounds.w, bounds.y],
    [bounds.x, bounds.y + bounds.h],
    [bounds.x + bounds.w, bounds.y + bounds.h],
  ]) {
    ctx.fillRect(cx - s / 2, cy - s / 2, s, s);
  }
  ctx.restore();
}

/**
 * Paint the whole document into *ctx* for the given view.
 *
 * Objects are painted in array order, which is z-order. Selection chrome
 * comes last so it is never buried under a later object.
 */
export function renderDoc(
  ctx: CanvasRenderingContext2D,
  doc: WbDoc,
  view: View,
  size: ViewportSize,
  services: RenderServices,
  /**
   * Device pixel ratio, folded into the view transform.
   *
   * It has to be folded in rather than applied around this call:
   * `setTransform` is absolute, so it discards any `scale()` the caller
   * set beforehand. Painting the committed objects without the ratio
   * while previewing the in-progress stroke with it puts the two in
   * different spaces, and the stroke jumps on release.
   */
  dpr = 1,
): void {
  const s = view.zoom * dpr;
  ctx.setTransform(s, 0, 0, s, -view.x * s, -view.y * s);
  ctx.clearRect(view.x, view.y, size.w / view.zoom, size.h / view.zoom);

  for (const obj of doc.objects) {
    switch (obj.kind) {
      case "ink":
        paintInk(ctx, obj);
        break;
      case "text":
        paintText(ctx, obj);
        break;
      case "draw":
        paintDraw(ctx, obj, services);
        break;
      case "formula":
        paintFormula(ctx, obj, services);
        break;
      case "artifact":
        paintArtifactFrame(ctx, obj);
        break;
      case "slot":
        paintSlot(ctx, obj);
        break;
      case "erase":
        paintErase(ctx, obj);
        break;
    }
  }

  for (const obj of doc.objects) {
    if (!obj.selected) continue;
    const bounds = objectBounds(obj, services);
    if (bounds) paintSelection(ctx, bounds, view.zoom);
  }
}
