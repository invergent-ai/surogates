// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A grid of figures: a list the grid names, one item per figure holding its
// label, value, hint and detail, and a caption under the list when given.
import { afterEach, describe, expect, it } from "vitest";

import { Figure, FigureGrid } from "../src/figure-grid";
import { cleanup, render } from "./dom";

afterEach(cleanup);

function listOf(root: ParentNode, name: string): HTMLElement {
  const list = Array.from(root.querySelectorAll<HTMLElement>("ul")).find(
    (candidate) => candidate.getAttribute("aria-label") === name,
  );
  if (!list) throw new Error(`no list named "${name}"`);
  return list;
}

describe("figure grid", () => {
  it("each figure is an item of the list the grid names", () => {
    const view = render(
      <FigureGrid label="Figures">
        <Figure label="Runs" value="1,204" hint="+12%" detail="Across 6 agents" />
        <Figure label="Failed" value="3" />
      </FigureGrid>,
    );
    const items = Array.from(listOf(view, "Figures").children);

    expect(items.map((item) => item.tagName)).toEqual(["LI", "LI"]);
    const [runs, failed] = items.map((item) => item.textContent ?? "");
    for (const text of ["Runs", "1,204", "+12%", "Across 6 agents"]) {
      expect(runs).toContain(text);
    }
    expect(failed).toContain("Failed");
    expect(failed).toContain("3");
    expect(failed).not.toContain("Runs");
  });

  it("a caption shows under the list, and none without one", () => {
    const view = render(
      <FigureGrid label="Figures" caption="Counted over the last 30 days">
        <Figure label="Runs" value="12" />
      </FigureGrid>,
    );
    const under = listOf(view, "Figures").nextElementSibling;

    expect(under?.textContent).toBe("Counted over the last 30 days");

    const bare = render(
      <FigureGrid label="Bare">
        <Figure label="Runs" value="12" />
      </FigureGrid>,
    );
    expect(listOf(bare, "Bare").nextElementSibling).toBeNull();
  });

  it("a figure with info shows a More info hint by its label", () => {
    const view = render(
      <FigureGrid label="Figures">
        <Figure label="Runs" value="12" info="Sessions started in the period" />
        <Figure label="Failed" value="3" />
      </FigureGrid>,
    );
    const [runs, failed] = Array.from(listOf(view, "Figures").children);

    expect(runs?.querySelector("[aria-label='More info']")).not.toBeNull();
    expect(failed?.querySelector("[aria-label='More info']")).toBeNull();
  });
});
