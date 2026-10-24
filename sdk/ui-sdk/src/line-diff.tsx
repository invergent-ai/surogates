// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Two texts compared line by line: each line kept, removed or added, in
// order, the removed and added ones in their tones. With `context`, a run of
// unchanged lines keeps only that many next to each change and folds the
// rest into one row. Ported from Studio's line diff, with two changes: the
// lines both texts share at their start and at their end are kept before the
// comparison, and a middle too large to compare reads as every old line
// removed, then every new line added.
"use client";

import { useMemo } from "react";

import { TONE } from "./chip";
import { cn } from "./lib/utils";

export type DiffRow = {
  change: "unchanged" | "added" | "removed";
  text: string;
};

/** The most cells the comparison's table may hold before it gives up. */
export const MAX_DIFF_CELLS = 4_000_000;

/** A text's lines; an empty text has none. */
function linesOf(text: string): string[] {
  return text.length ? text.split("\n") : [];
}

/** The rows for two runs of lines that share no start and no end. */
function compare(a: readonly string[], b: readonly string[]): DiffRow[] {
  const n = a.length;
  const m = b.length;
  const rows: DiffRow[] = [];
  if (n * m > MAX_DIFF_CELLS) {
    for (const text of a) rows.push({ change: "removed", text });
    for (const text of b) rows.push({ change: "added", text });
    return rows;
  }
  // The longest common run of lines from (i, j) on sits at i * (m + 1) + j.
  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] =
        a[i] === b[j]
          ? lcs[(i + 1) * width + j + 1] + 1
          : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ change: "unchanged", text: a[i] });
      i++;
      j++;
    } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
      rows.push({ change: "removed", text: a[i] });
      i++;
    } else {
      rows.push({ change: "added", text: b[j] });
      j++;
    }
  }
  while (i < n) rows.push({ change: "removed", text: a[i++] });
  while (j < m) rows.push({ change: "added", text: b[j++] });
  return rows;
}

/** Each line of `before` and `after`, kept, removed or added, in order. */
export function diffLines(before: string, after: string): DiffRow[] {
  const a = linesOf(before);
  const b = linesOf(after);
  // The end is looked for only in the lines the start left, so no line counts
  // as both.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start++;
  }
  let end = 0;
  while (
    end < a.length - start &&
    end < b.length - start &&
    a[a.length - 1 - end] === b[b.length - 1 - end]
  ) {
    end++;
  }

  const rows: DiffRow[] = [];
  for (let k = 0; k < start; k++) {
    rows.push({ change: "unchanged", text: a[k] });
  }
  for (const row of compare(
    a.slice(start, a.length - end),
    b.slice(start, b.length - end),
  )) {
    rows.push(row);
  }
  for (let k = a.length - end; k < a.length; k++) {
    rows.push({ change: "unchanged", text: a[k] });
  }
  return rows;
}

type Shown = { row: DiffRow } | { skipped: string };

/** The rows to draw: unchanged runs folded to `context` rows by each change. */
function fold(rows: readonly DiffRow[], context: number | undefined): Shown[] {
  if (context === undefined) return rows.map((row) => ({ row }));
  if (rows.every((row) => row.change === "unchanged")) {
    return [{ skipped: "No changes" }];
  }
  const shown: Shown[] = [];
  let k = 0;
  while (k < rows.length) {
    if (rows[k].change !== "unchanged") {
      shown.push({ row: rows[k] });
      k++;
      continue;
    }
    let end = k;
    while (end < rows.length && rows[end].change === "unchanged") end++;
    // A run keeps `context` rows after the change before it and before the
    // change after it.
    const head = k > 0 ? Math.min(context, end - k) : 0;
    const tail = end < rows.length ? Math.min(context, end - k - head) : 0;
    const hidden = end - k - head - tail;
    for (let at = k; at < k + head; at++) shown.push({ row: rows[at] });
    if (hidden > 0) {
      shown.push({
        skipped: `… ${hidden} unchanged ${hidden === 1 ? "line" : "lines"}`,
      });
    }
    for (let at = end - tail; at < end; at++) shown.push({ row: rows[at] });
    k = end;
  }
  return shown;
}

const MARKER: Record<DiffRow["change"], string> = {
  unchanged: " ",
  added: "+",
  removed: "-",
};

export function LineDiff({
  before,
  after,
  context,
  className,
}: {
  before: string;
  after: string;
  /** Given, each unchanged run keeps this many lines next to each change. */
  context?: number;
  className?: string;
}) {
  const rows = useMemo(() => diffLines(before, after), [before, after]);
  const shown = useMemo(() => fold(rows, context), [rows, context]);
  return (
    <pre
      data-slot="line-diff"
      className={cn(
        "text-xs leading-relaxed overflow-x-auto rounded-md border border-border/60",
        className,
      )}
    >
      {shown.map((item, idx) =>
        "skipped" in item ? (
          <div
            key={idx}
            data-change="skipped"
            className={cn("px-3 whitespace-pre-wrap", TONE.muted)}
          >
            {item.skipped}
          </div>
        ) : (
          <div
            key={idx}
            data-change={item.row.change}
            className={cn(
              "px-3 whitespace-pre-wrap",
              item.row.change === "added" && TONE.ok,
              item.row.change === "removed" && TONE.bad,
              item.row.change === "unchanged" && "text-muted-foreground",
            )}
          >
            <span aria-hidden className="mr-2 select-none opacity-50">
              {MARKER[item.row.change]}
            </span>
            {item.row.change !== "unchanged" && (
              <span className="sr-only">
                {item.row.change === "added" ? "Added: " : "Removed: "}
              </span>
            )}
            {item.row.text || " "}
          </div>
        ),
      )}
    </pre>
  );
}
