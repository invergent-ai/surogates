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
import { CANVAS_SIZE, type WbDoc } from "./doc";
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
 * Priority: the latest input if there is one, else all content, else the
 * current viewport. Whatever is chosen is padded, floored to a minimum
 * span and clamped inside the canvas.
 */
export function planAtlas(
  doc: WbDoc,
  latest: Rect | null,
  view: View,
  viewport: ViewportSize,
  services: RenderServices,
): AtlasPlan {
  const base =
    latest ?? contentBounds(doc, services) ?? viewportRect(view, viewport);

  // Grow to the minimum span about the base's centre, then add margin,
  // so a 4px tick mark still arrives with context around it.
  const cx = base.x + base.w / 2;
  const cy = base.y + base.h / 2;
  const spanW = Math.max(base.w * (1 + CAPTURE_MARGIN * 2), MIN_CAPTURE_SPAN);
  const spanH = Math.max(base.h * (1 + CAPTURE_MARGIN * 2), MIN_CAPTURE_SPAN);

  const w = Math.min(spanW, CANVAS_SIZE);
  const h = Math.min(spanH, CANVAS_SIZE);
  const x = clamp(cx - w / 2, 0, CANVAS_SIZE - w);
  const y = clamp(cy - h / 2, 0, CANVAS_SIZE - h);
  const sourceRect: Rect = { x, y, w, h };

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
  return canvas;
}

export interface AtlasExtras {
  mode?: "sketch" | "deep";
  selection?: Rect | null;
  typedInput?: string;
}

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
    canvasSize: CANVAS_SIZE,
    mode: extras.mode === "deep" ? "deep" : "sketch",
  };
  if (latest) meta.latestInput = latest;
  if (hotspots.length > 0) meta.hotspots = hotspots;
  if (extras.selection) meta.selection = extras.selection;
  if (extras.typedInput?.trim()) meta.typedInput = extras.typedInput.trim();
  return meta;
}
