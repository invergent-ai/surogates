// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Unified inline diff viewer component using the `diff` package.
// Renders removed lines with a red background and added lines with
// a green background. Within modified line pairs, individual changed
// words get a stronger highlight layered on top. Unchanged regions
// beyond the context window are collapsible.

import { memo, useMemo, useState, useCallback } from "react";
import { diffLines, diffWordsWithSpace } from "diff";
import type { Change } from "diff";
import { cn } from "../../lib/utils";

// ── Types ──────────────────────────────────────────────────────────

interface DiffLine {
  type: "added" | "removed" | "unchanged";
  content: string;
  lineNum: number;
}

interface DiffViewerProps {
  /** Original text (left side). */
  oldValue: string;
  /** Modified text (right side). */
  newValue: string;
  /** Number of unchanged context lines to show around changes. */
  contextLines?: number;
  /** Optional filename to display in the header. */
  fileName?: string;
  /** Whether to show word-level highlighting within changed lines. */
  wordDiff?: boolean;
  /** Additional CSS classes for the outer container. */
  className?: string;
}

// ── Diff computation ───────────────────────────────────────────────

/**
 * Split a Change value into individual line strings, stripping the
 * trailing newline that `diffLines` appends to each chunk.
 */
function splitChangeLines(change: Change): string[] {
  const v = change.value;
  if (v === "") return [];
  const stripped = v.endsWith("\n") ? v.slice(0, -1) : v;
  return stripped.split("\n");
}

/**
 * Build a flat array of DiffLines from the raw Change[] produced by
 * `diffLines`. Each line gets a sequential line number (tracking
 * position in the combined output).
 */
function buildDiffLines(changes: Change[]): DiffLine[] {
  const lines: DiffLine[] = [];
  let lineNum = 1;

  for (const change of changes) {
    const type: DiffLine["type"] = change.added
      ? "added"
      : change.removed
        ? "removed"
        : "unchanged";

    for (const text of splitChangeLines(change)) {
      lines.push({ type, content: text, lineNum: lineNum++ });
    }
  }

  return lines;
}

// ── Sections (context collapsing) ──────────────────────────────────

interface DiffSection {
  kind: "lines" | "collapsed";
  lines: DiffLine[];
  hiddenCount?: number;
}

function buildSections(allLines: DiffLine[], contextLines: number): DiffSection[] {
  if (allLines.length === 0) return [];

  const visible = new Uint8Array(allLines.length);
  for (let i = 0; i < allLines.length; i++) {
    if (allLines[i].type !== "unchanged") {
      const lo = Math.max(0, i - contextLines);
      const hi = Math.min(allLines.length - 1, i + contextLines);
      for (let j = lo; j <= hi; j++) visible[j] = 1;
    }
  }

  const sections: DiffSection[] = [];
  let i = 0;
  while (i < allLines.length) {
    if (visible[i]) {
      const start = i;
      while (i < allLines.length && visible[i]) i++;
      sections.push({ kind: "lines", lines: allLines.slice(start, i) });
    } else {
      const start = i;
      while (i < allLines.length && !visible[i]) i++;
      sections.push({ kind: "collapsed", lines: allLines.slice(start, i), hiddenCount: i - start });
    }
  }

  return sections;
}

// ── Word-diff pairing ──────────────────────────────────────────────

/**
 * Compute the ratio of common characters between two strings (0..1).
 * When the ratio is low the lines are essentially unrelated and
 * word-level highlighting just paints everything, which is noisy.
 */
function similarity(a: string, b: string): number {
  if (a.length === 0 && b.length === 0) return 1;
  if (a.length === 0 || b.length === 0) return 0;
  const changes = diffWordsWithSpace(a, b);
  let common = 0;
  let total = 0;
  for (const c of changes) {
    total += c.value.length;
    if (!c.added && !c.removed) common += c.value.length;
  }
  return total > 0 ? common / total : 0;
}

/** Minimum similarity (0..1) for word-level highlighting to activate. */
const WORD_DIFF_THRESHOLD = 0.35;

/**
 * Pair consecutive removed+added runs for word-level diffing.
 * Only pairs lines that are sufficiently similar — entirely
 * different lines are left unpaired so they render without
 * per-word highlights.
 * Returns a map: line array index → paired line content from opposite side.
 */
function buildWordDiffPairs(lines: DiffLine[]): Map<number, string> {
  const pairs = new Map<number, string>();
  let i = 0;
  while (i < lines.length) {
    const removedStart = i;
    while (i < lines.length && lines[i].type === "removed") i++;
    const removedEnd = i;

    const addedStart = i;
    while (i < lines.length && lines[i].type === "added") i++;
    const addedEnd = i;

    const removedCount = removedEnd - removedStart;
    const addedCount = addedEnd - addedStart;
    const pairCount = Math.min(removedCount, addedCount);
    for (let j = 0; j < pairCount; j++) {
      const oldContent = lines[removedStart + j].content;
      const newContent = lines[addedStart + j].content;
      if (similarity(oldContent, newContent) >= WORD_DIFF_THRESHOLD) {
        pairs.set(removedStart + j, newContent);
        pairs.set(addedStart + j, oldContent);
      }
    }

    if (i < lines.length && lines[i].type === "unchanged") i++;
  }
  return pairs;
}

// ── Word-diff rendering ────────────────────────────────────────────

/**
 * Render word-level diff spans. Changed tokens get a stronger
 * highlight that layers on top of the line-level background.
 */
function renderWordDiff(
  oldText: string,
  newText: string,
  side: "old" | "new",
): React.ReactNode[] {
  const changes = diffWordsWithSpace(oldText, newText);
  const spans: React.ReactNode[] = [];

  for (let i = 0; i < changes.length; i++) {
    const c = changes[i];
    if (side === "old") {
      if (c.added) continue;
      spans.push(
        c.removed ? (
          <span key={i} className="bg-red-500/30 rounded-sm">{c.value}</span>
        ) : (
          <span key={i}>{c.value}</span>
        ),
      );
    } else {
      if (c.removed) continue;
      spans.push(
        c.added ? (
          <span key={i} className="bg-green-500/30 rounded-sm">{c.value}</span>
        ) : (
          <span key={i}>{c.value}</span>
        ),
      );
    }
  }

  return spans;
}

// ── Sub-components ─────────────────────────────────────────────────

function DiffLineRow({
  line,
  wordDiffContent,
}: {
  line: DiffLine;
  wordDiffContent?: React.ReactNode[];
}) {
  const bgClass =
    line.type === "added"
      ? "bg-green-500/15"
      : line.type === "removed"
        ? "bg-red-500/15"
        : "";

  const textClass =
    line.type === "added"
      ? "text-green-800 dark:text-green-300"
      : line.type === "removed"
        ? "text-red-800 dark:text-red-300"
        : "text-foreground/80";

  const gutterClass =
    line.type === "added"
      ? "text-green-500/40 bg-green-500/10"
      : line.type === "removed"
        ? "text-red-500/40 bg-red-500/10"
        : "text-muted-foreground/30";

  return (
    <div className={cn("flex text-sm leading-5", bgClass)}>
      <span
        className={cn(
          "w-10 shrink-0 select-none text-right pr-2 tabular-nums",
          gutterClass,
        )}
      >
        {line.lineNum}
      </span>
      <span className={cn("flex-1 whitespace-pre-wrap break-all px-3", textClass)}>
        {wordDiffContent ?? line.content}
      </span>
    </div>
  );
}

function CollapsedBlock({
  count,
  onExpand,
}: {
  count: number;
  onExpand: () => void;
}) {
  return (
    <div className="flex text-sm leading-5 bg-muted/30 border-y border-border/30">
      <span className="w-10 shrink-0" />
      <button
        type="button"
        onClick={onExpand}
        className="flex-1 text-left px-3 py-0.5 text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
      >
        ↕ {count} unchanged line{count !== 1 ? "s" : ""}
      </button>
    </div>
  );
}

// ── Stats ──────────────────────────────────────────────────────────

function DiffStats({ lines }: { lines: DiffLine[] }) {
  let additions = 0;
  let deletions = 0;
  for (const l of lines) {
    if (l.type === "added") additions++;
    if (l.type === "removed") deletions++;
  }

  if (additions === 0 && deletions === 0) return null;

  return (
    <span className="text-sm text-muted-foreground font-mono">
      {additions > 0 && (
        <span className="text-green-600 dark:text-green-400">+{additions}</span>
      )}
      {additions > 0 && deletions > 0 && " "}
      {deletions > 0 && (
        <span className="text-red-600 dark:text-red-400">−{deletions}</span>
      )}
    </span>
  );
}

// ── Header label ───────────────────────────────────────────────────

function HeaderLabel({ lines }: { lines: DiffLine[] }) {
  const additions = lines.filter((l) => l.type === "added").length;
  const deletions = lines.filter((l) => l.type === "removed").length;

  const parts: string[] = [];
  if (additions > 0 && deletions > 0) {
    parts.push(`Modified ${Math.max(additions, deletions)} line${Math.max(additions, deletions) !== 1 ? "s" : ""}`);
  } else if (additions > 0) {
    parts.push(`Added ${additions} line${additions !== 1 ? "s" : ""}`);
  } else if (deletions > 0) {
    parts.push(`Removed ${deletions} line${deletions !== 1 ? "s" : ""}`);
  }

  if (parts.length === 0) return null;

  return (
    <span className="text-xs text-muted-foreground text-muted-foregound">{parts[0]}</span>
  );
}

// ── Main component ─────────────────────────────────────────────────

export const DiffViewer = memo(function DiffViewer({
  oldValue,
  newValue,
  contextLines = 3,
  fileName,
  wordDiff: enableWordDiff = true,
  className,
}: DiffViewerProps) {
  const diffResult = useMemo(() => {
    const changes = diffLines(oldValue, newValue);
    const lines = buildDiffLines(changes);
    const sections = buildSections(lines, contextLines);
    return { lines, sections };
  }, [oldValue, newValue, contextLines]);

  const [expandedSections, setExpandedSections] = useState<Set<number>>(
    () => new Set(),
  );

  const toggleSection = useCallback((idx: number) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  }, []);

  if (oldValue === newValue) {
    return (
      <div className={cn("rounded border bg-muted/30 p-3 text-sm text-muted-foreground font-mono", className)}>
        No changes
      </div>
    );
  }

  return (
    <div className={cn("space-y-1", className)}>
      {/* Summary label above the diff block */}
      <HeaderLabel lines={diffResult.lines} />

      <div className="rounded border border-border bg-muted/20 overflow-hidden font-mono">
        {/* File header with stats */}
        {fileName && (
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/50 bg-muted/40">
            <span className="text-sm text-foreground/80 truncate">{fileName}</span>
            <DiffStats lines={diffResult.lines} />
          </div>
        )}

        {/* Diff body */}
        <div className="overflow-x-auto">
          {diffResult.sections.map((section, sIdx) => {
            if (section.kind === "collapsed" && !expandedSections.has(sIdx)) {
              return (
                <CollapsedBlock
                  key={sIdx}
                  count={section.hiddenCount!}
                  onExpand={() => toggleSection(sIdx)}
                />
              );
            }

            const wordPairs = enableWordDiff
              ? buildWordDiffPairs(section.lines)
              : null;

            return (
              <div key={sIdx}>
                {section.lines.map((line, lIdx) => {
                  let wordContent: React.ReactNode[] | undefined;
                  if (wordPairs?.has(lIdx)) {
                    const pairedContent = wordPairs.get(lIdx)!;
                    if (line.type === "removed") {
                      wordContent = renderWordDiff(line.content, pairedContent, "old");
                    } else if (line.type === "added") {
                      wordContent = renderWordDiff(pairedContent, line.content, "new");
                    }
                  }
                  return (
                    <DiffLineRow
                      key={`${sIdx}-${lIdx}`}
                      line={line}
                      wordDiffContent={wordContent}
                    />
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
});
