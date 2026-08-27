// Typed access to the vendored PenEcho UMD modules. The .js files are
// verbatim upstream copies and carry no types of their own; this barrel
// is the only place that asserts their shape, so a re-copy that changes
// an export surfaces here rather than at twenty call sites.
//
// See ./README.md for provenance and licence.

// @ts-expect-error -- untyped vendored UMD module
import drawModule from "./draw.js";
// @ts-expect-error -- untyped vendored UMD module
import mixedTextModule from "./mixed-text.js";
// @ts-expect-error -- untyped vendored UMD module
import selectionModule from "./selection.js";

export interface DrawBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface NormalizedDraw {
  width: number;
  tension: number;
  _draw: { bounds: DrawBounds; primitives: unknown[] };
}

export interface RenderedDraw {
  image: HTMLCanvasElement;
  x: number;
  y: number;
}

export interface DrawPoint {
  x: number;
  y: number;
}

export interface DrawApi {
  /** Validate and normalize a `draw` command. Returns null when invalid. */
  normalize(command: unknown, canvasSize?: number): NormalizedDraw | null;
  /** Rasterise a `draw` command. Returns null when invalid. */
  render(
    command: unknown,
    createCanvas: (w: number, h: number) => HTMLCanvasElement,
    color?: string,
  ): RenderedDraw | null;
  smoothSegments(
    points: DrawPoint[],
    closed: boolean,
    tension: number,
  ): unknown[];
}

export const DRAW = drawModule as DrawApi;
export const MIXED_TEXT = mixedTextModule as Record<string, unknown>;
export const SELECTION = selectionModule as Record<string, unknown>;
