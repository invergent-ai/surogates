// Where each window opens: where the user left it, as Claude Desktop keeps its main
// window's place (electron-window-state). One file for every window, by key.

import { readState, writeState } from "./state-file.js";

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

// No x and y: the window is centred.
export type Place = Partial<Bounds> & { width: number; height: number; maximized: boolean };

const within = (inner: Bounds, outer: Bounds): boolean =>
  inner.x >= outer.x && inner.y >= outer.y &&
  inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height;

export class WindowStates {
  constructor(private readonly path: string) {}

  /** Where window *key* opens: as it was left, while that lies whole on one of *displays*; else at *size*, centred. */
  restore(key: string, size: { width: number; height: number }, displays: Bounds[]): Place {
    const saved = this.saved()[key];
    if (saved && displays.some((display) => within(saved, display))) return saved;
    return { ...size, maximized: false };
  }

  save(key: string, bounds: Bounds, maximized: boolean): void {
    writeState(this.path, { ...this.saved(), [key]: { ...bounds, maximized } });
  }

  private saved(): Record<string, Bounds & { maximized: boolean }> {
    return readState(this.path, {});
  }
}
