// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Two texts compared line by line: what was removed and added, in order, the
// unchanged runs folded to the context around each change, and the words a
// screen reader says for each changed line.
import { afterEach, describe, expect, it } from "vitest";

import { diffLines, LineDiff } from "../src/line-diff";
import { cleanup, render } from "./dom";

afterEach(cleanup);

/** The diff's rows, skipped runs included, in order. */
function rowsOf(view: HTMLElement): HTMLElement[] {
  return Array.from(
    view.querySelectorAll<HTMLElement>("[data-slot=line-diff] > [data-change]"),
  );
}

/** What a row says, without the marker that only the eye reads. */
function spoken(row: HTMLElement): string {
  const copy = row.cloneNode(true) as HTMLElement;
  for (const hidden of copy.querySelectorAll("[aria-hidden]")) hidden.remove();
  return copy.textContent ?? "";
}

function lines(prefix: string, count: number): string[] {
  return Array.from({ length: count }, (_, i) => `${prefix} ${i + 1}`);
}

describe("line diff", () => {
  it("lines added, removed and kept show in order", () => {
    expect(diffLines("a\nb\nc", "a\nc\nd")).toEqual([
      { change: "unchanged", text: "a" },
      { change: "removed", text: "b" },
      { change: "unchanged", text: "c" },
      { change: "added", text: "d" },
    ]);
    expect(diffLines("", "x")).toEqual([{ change: "added", text: "x" }]);
    // A line kept at the start is not kept again at the end.
    expect(diffLines("a\na", "a")).toEqual([
      { change: "unchanged", text: "a" },
      { change: "removed", text: "a" },
    ]);
  });

  it("a long text with one change keeps only the context around it", () => {
    const before = lines("line", 100);
    const after = before.map((line) => (line === "line 50" ? "changed" : line));
    const view = render(
      <LineDiff
        before={before.join("\n")}
        after={after.join("\n")}
        context={3}
      />,
    );
    const rows = rowsOf(view);

    expect(rows.map((row) => row.getAttribute("data-change"))).toEqual([
      "skipped",
      "unchanged",
      "unchanged",
      "unchanged",
      "removed",
      "added",
      "unchanged",
      "unchanged",
      "unchanged",
      "skipped",
    ]);
    expect(rows[0].textContent).toBe("… 46 unchanged lines");
    expect(rows[9].textContent).toBe("… 47 unchanged lines");
    expect(spoken(rows[3])).toBe("line 49");
    expect(spoken(rows[6])).toBe("line 51");
  });

  it("two large texts that differ everywhere show all removed, then all added", () => {
    const before = lines("a", 3000);
    const after = lines("b", 3000);

    const rows = diffLines(before.join("\n"), after.join("\n"));

    expect(rows).toHaveLength(6000);
    expect(rows.slice(0, 3000)).toEqual(
      before.map((text) => ({ change: "removed", text })),
    );
    expect(rows.slice(3000)).toEqual(
      after.map((text) => ({ change: "added", text })),
    );

    // Too large to compare, so even a line both share in the middle is not
    // looked for.
    const sharing = (text: string, i: number) => (i === 1500 ? "shared" : text);
    const shared = diffLines(
      before.map(sharing).join("\n"),
      after.map(sharing).join("\n"),
    );
    expect(shared).toHaveLength(6000);
    expect(shared.filter((row) => row.change === "unchanged")).toEqual([]);
  });

  it("equal texts read no changes when only the context is shown, and in full otherwise", () => {
    const folded = rowsOf(render(<LineDiff before="x" after="x" context={3} />));
    expect(folded).toHaveLength(1);
    expect(folded[0].getAttribute("data-change")).toBe("skipped");
    expect(folded[0].textContent).toBe("No changes");
    cleanup();

    const whole = rowsOf(render(<LineDiff before="x" after="x" />));
    expect(whole).toHaveLength(1);
    expect(whole[0].getAttribute("data-change")).toBe("unchanged");
    expect(spoken(whole[0])).toBe("x");
  });

  it("a screen reader hears which lines were added and removed", () => {
    const rows = rowsOf(render(<LineDiff before="a" after="b" />));

    expect(rows.map((row) => row.getAttribute("data-change"))).toEqual([
      "removed",
      "added",
    ]);
    expect(spoken(rows[0])).toBe("Removed: a");
    expect(spoken(rows[1])).toBe("Added: b");
  });
});
