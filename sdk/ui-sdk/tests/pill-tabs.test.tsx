// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A strip of section tabs that is wider than its box: the strip scrolls to
// bring the active tab into view, and nothing else on the page moves, neither
// on the first paint nor when another tab is picked.
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PillTabs } from "../src/pill-tabs";
import { cleanup, render, rerender } from "./dom";

const STRIP_WIDTH = 300;
const PILL_WIDTH = 100;
const IDS = Array.from({ length: 10 }, (_, i) => `tab-${i}`);
const TABS = IDS.map((id) => ({ id, label: id }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/**
 * Gives the strip a box of `STRIP_WIDTH` and each tab a box of `PILL_WIDTH`,
 * laid end to end: a tab's box moves left as the strip scrolls right, the way
 * a browser reports it. The page has no layout engine, so the boxes are
 * supplied here.
 */
beforeEach(() => {
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      const box = (left: number, width: number) =>
        ({
          left,
          width,
          right: left + width,
          top: 0,
          bottom: 40,
          height: 40,
          x: left,
          y: 0,
          toJSON: () => ({}),
        }) as DOMRect;
      if (this.getAttribute("role") === "tablist") return box(0, STRIP_WIDTH);
      if (this.getAttribute("role") === "tab") {
        const strip = this.closest("[role=tablist]");
        const index = Array.from(strip?.children ?? []).indexOf(this);
        return box(index * PILL_WIDTH - (strip?.scrollLeft ?? 0), PILL_WIDTH);
      }
      return box(0, 0);
    },
  );
});

/** Lets a smooth scroll finish. */
async function finishScrolling(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

/** The strip of `root`. */
function stripOf(root: ParentNode): HTMLElement {
  const strip = root.querySelector<HTMLElement>("[role=tablist]");
  if (!strip) throw new Error("no tablist");
  return strip;
}

/** True when the whole of tab `index` lies within what the strip shows. */
function inView(strip: HTMLElement, index: number): boolean {
  const start = index * PILL_WIDTH;
  return (
    start >= strip.scrollLeft &&
    start + PILL_WIDTH <= strip.scrollLeft + STRIP_WIDTH
  );
}

/** The tabs as a page lays them out: inside a box of their own. */
function Page({ activeId }: { activeId: string }) {
  return (
    <section>
      <PillTabs tabs={TABS} activeId={activeId} onSelect={() => {}} />
    </section>
  );
}

describe("pill tabs", () => {
  it("a tab far along is brought into view by moving the strip alone", () => {
    const intoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const pageScroll = vi.spyOn(window, "scrollTo");
    const root = render(<Page activeId="tab-8" />);
    const strip = stripOf(root);

    expect(strip.scrollLeft).toBeGreaterThan(0);
    expect(inView(strip, 8)).toBe(true);
    expect(intoView).not.toHaveBeenCalled();
    expect(pageScroll).not.toHaveBeenCalled();
  });

  it("the page keeps its scroll on the first paint", () => {
    document.documentElement.scrollTop = 347;
    const root = render(<Page activeId="tab-9" />);

    expect(inView(stripOf(root), 9)).toBe(true);
    expect(document.documentElement.scrollTop).toBe(347);
    document.documentElement.scrollTop = 0;
  });

  it("a first tab already in view does not move the strip", () => {
    const scrolled = vi.spyOn(Element.prototype, "scrollTo");
    const root = render(<Page activeId="tab-1" />);

    expect(stripOf(root).scrollLeft).toBe(0);
    expect(scrolled).not.toHaveBeenCalled();
  });

  it("picking another tab brings it into view without moving the page", async () => {
    const intoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const pageScroll = vi.spyOn(window, "scrollTo");
    document.documentElement.scrollTop = 347;
    const root = render(<Page activeId="tab-0" />);
    const strip = stripOf(root);
    expect(strip.scrollLeft).toBe(0);

    rerender(<Page activeId="tab-7" />);
    await finishScrolling();

    expect(inView(strip, 7)).toBe(true);
    expect(document.documentElement.scrollTop).toBe(347);
    expect(intoView).not.toHaveBeenCalled();
    expect(pageScroll).not.toHaveBeenCalled();
    document.documentElement.scrollTop = 0;
  });

  it("a page that renders again on the same tab does not pull the strip back", async () => {
    const root = render(<Page activeId="tab-8" />);
    const strip = stripOf(root);
    strip.scrollLeft = 0;

    rerender(<Page activeId="tab-8" />);
    await finishScrolling();

    expect(strip.scrollLeft).toBe(0);
  });
});
