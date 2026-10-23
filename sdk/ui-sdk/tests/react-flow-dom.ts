// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// What React Flow needs from a page that lays nothing out, after its testing
// guide. React Flow measures each node and the canvas through a
// ResizeObserver and `offsetWidth`/`offsetHeight`, and draws nodes and edges
// only once they are measured. The test page never lays out, so its observer
// never reports and every size is 0. The test page already brings the rest
// the guide stands in for: `DOMMatrixReadOnly` reads the zoom, and an SVG
// text has a `getBBox`. Every test file that renders a flow calls
// `standInForReactFlow` first, then waits for React to settle.

/**
 * A ResizeObserver that reports each element once, right after it is
 * observed, at the size its `offsetWidth` and `offsetHeight` give. Like a
 * browser's, it reports after the current task rather than inside `observe`:
 * React Flow observes a node before the canvas around it is ready.
 */
class MeasuringResizeObserver {
  private readonly observed = new Set<Element>();

  constructor(private readonly callback: ResizeObserverCallback) {}

  observe(target: Element): void {
    this.observed.add(target);
    queueMicrotask(() => {
      if (!this.observed.has(target)) return;
      const { offsetWidth: width, offsetHeight: height } =
        target as HTMLElement;
      const contentRect = new DOMRect(0, 0, width, height);
      this.callback(
        [{ target, contentRect } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    });
  }

  unobserve(target: Element): void {
    this.observed.delete(target);
  }

  disconnect(): void {
    this.observed.clear();
  }
}

/** A size from the element's inline style, or 1 when it has none. */
function inlineSize(axis: "width" | "height") {
  return {
    configurable: true,
    get(this: HTMLElement): number {
      return Number.parseFloat(this.style[axis]) || 1;
    },
  };
}

let installed = false;

export function standInForReactFlow(): void {
  if (installed) return;
  installed = true;
  globalThis.ResizeObserver =
    MeasuringResizeObserver as unknown as typeof ResizeObserver;
  Object.defineProperties(HTMLElement.prototype, {
    offsetWidth: inlineSize("width"),
    offsetHeight: inlineSize("height"),
  });
}
