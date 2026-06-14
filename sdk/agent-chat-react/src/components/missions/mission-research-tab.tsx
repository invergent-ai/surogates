// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Research tab — the Arbor Idea Tree behind an `/auto-research` mission.
// A scores header (dev baseline→trunk and the authoritative held-out
// test) sits above the hypothesis tree: one indented row per idea node,
// dotted-decimal ordered, with status, dev score + delta, and the
// backpropagated insight. Shown only for research missions; the
// dashboard probes `getMissionResearch` and hides this tab on 404.
import { useMemo } from "react";

import type {
  AgentChatIdeaNode,
  AgentChatMissionResearch,
  AgentChatResearchRun,
} from "../../types";


export interface MissionResearchTabProps {
  research: AgentChatMissionResearch;
}


/** ROOT first, then dotted-decimal numeric order ("2" before "10",
 * "1.2" before "1.10") — mirrors the store's `_node_sort_key`. */
function compareNodes(a: AgentChatIdeaNode, b: AgentChatIdeaNode): number {
  if (a.nodeKey === b.nodeKey) return 0;
  if (a.nodeKey === "ROOT") return -1;
  if (b.nodeKey === "ROOT") return 1;
  const pa = a.nodeKey.split(".").map((p) => Number.parseInt(p, 10));
  const pb = b.nodeKey.split(".").map((p) => Number.parseInt(p, 10));
  const n = Math.min(pa.length, pb.length);
  for (let i = 0; i < n; i += 1) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return pa.length - pb.length;
}


function statusToneClass(status: string): string {
  switch (status) {
    case "merged":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400";
    case "done":
      return "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400";
    case "running":
      return "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400";
    case "failed":
      return "border-destructive/30 bg-destructive/10 text-destructive";
    case "pruned":
      return "border-border bg-muted/40 text-muted-foreground/70 line-through";
    default:
      return "border-border bg-muted/30 text-muted-foreground/80";
  }
}


/** Trim a score to ≤3 decimals; em-dash for missing. */
function fmtScore(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(3).replace(/\.?0+$/, "") || "0";
}


/** Signed delta vs. a reference, sign-flipped for minimize runs. */
function deltaLabel(
  score: number | null,
  ref: number | null,
  direction: string,
): { text: string; good: boolean } | null {
  if (score === null || score === undefined || ref === null || ref === undefined) {
    return null;
  }
  const raw = score - ref;
  const good = direction === "minimize" ? raw < 0 : raw > 0;
  const sign = raw > 0 ? "+" : raw < 0 ? "−" : "±";
  return { text: `${sign}${fmtScore(Math.abs(raw))}`, good };
}


function ScorePair({
  label,
  from,
  to,
  direction,
}: {
  label: string;
  from: number | null;
  to: number | null;
  direction: string;
}) {
  const delta = deltaLabel(to, from, direction);
  return (
    <div className="space-y-1">
      <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground/60">
        {label}
      </div>
      <div className="flex items-baseline gap-1.5 tabular-nums">
        <span className="text-muted-foreground/70">{fmtScore(from)}</span>
        <span className="text-muted-foreground/40">→</span>
        <span className="text-lg font-semibold text-foreground">
          {fmtScore(to)}
        </span>
        {delta ? (
          <span
            className={
              delta.good
                ? "text-xs font-medium text-emerald-600 dark:text-emerald-400"
                : "text-xs font-medium text-muted-foreground/60"
            }
          >
            {delta.text}
          </span>
        ) : null}
      </div>
    </div>
  );
}


function RunHeader({ run }: { run: AgentChatResearchRun }) {
  return (
    <section className="space-y-4 rounded-md border bg-card/40 p-4">
      {run.objective ? (
        <p className="text-sm font-medium text-foreground/90">
          {run.objective}
        </p>
      ) : null}
      <div className="grid grid-cols-2 gap-4">
        <ScorePair
          label="Dev (working)"
          from={run.baselineScore}
          to={run.trunkScore ?? run.baselineScore}
          direction={run.metricDirection}
        />
        <ScorePair
          label="Held-out test (authoritative)"
          from={run.testBaselineScore}
          to={run.testTrunkScore ?? run.testBaselineScore}
          direction={run.metricDirection}
        />
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[11px]">
        <dt className="text-muted-foreground/60">Status</dt>
        <dd className="text-foreground/80">{run.status}</dd>
        <dt className="text-muted-foreground/60">Repo</dt>
        <dd>
          <code className="text-[11px]">{run.repoPath}</code>
        </dd>
        <dt className="text-muted-foreground/60">Trunk</dt>
        <dd>
          <code className="text-[11px]">{run.trunkBranch}</code>
        </dd>
        {run.evalCmd ? (
          <>
            <dt className="text-muted-foreground/60">Dev eval</dt>
            <dd>
              <code className="text-[11px]">{run.evalCmd}</code>
            </dd>
          </>
        ) : null}
        {run.evalCmdTest ? (
          <>
            <dt className="text-muted-foreground/60">Held-out eval</dt>
            <dd>
              <code className="text-[11px]">{run.evalCmdTest}</code>
            </dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}


function IdeaRow({
  node,
  baseline,
  direction,
}: {
  node: AgentChatIdeaNode;
  baseline: number | null;
  direction: string;
}) {
  const indent = node.nodeKey === "ROOT" ? 0 : node.depth;
  const delta = deltaLabel(node.score, baseline, direction);
  const headline = (node.hypothesis || "").split("\n")[0];
  const insight = node.insight ? node.insight.split("\n").slice(-1)[0] : null;
  return (
    <li
      className="flex flex-col gap-0.5 border-b border-border/40 py-2 last:border-b-0"
      style={{ paddingLeft: `${indent * 16}px` }}
    >
      <div className="flex items-center gap-2">
        <code className="shrink-0 text-[11px] text-muted-foreground/60">
          {node.nodeKey}
        </code>
        <span
          className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider ${statusToneClass(node.status)}`}
        >
          {node.status}
        </span>
        {node.score !== null && node.score !== undefined ? (
          <span className="shrink-0 tabular-nums text-xs font-medium text-foreground/90">
            {fmtScore(node.score)}
            {delta ? (
              <span
                className={
                  delta.good
                    ? " text-emerald-600 dark:text-emerald-400"
                    : " text-muted-foreground/50"
                }
              >
                {" "}
                {delta.text}
              </span>
            ) : null}
          </span>
        ) : null}
        <span className="truncate text-xs text-foreground/80" title={headline}>
          {headline}
        </span>
      </div>
      {insight ? (
        <p className="truncate pl-[calc(1.5rem)] text-[11px] text-muted-foreground/60" title={node.insight ?? undefined}>
          {insight}
        </p>
      ) : null}
    </li>
  );
}


export function MissionResearchTab({ research }: MissionResearchTabProps) {
  const sorted = useMemo(
    () => [...research.nodes].sort(compareNodes),
    [research.nodes],
  );
  const { run } = research;
  return (
    <div className="space-y-4">
      <RunHeader run={run} />
      <section>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
          Idea Tree · {sorted.length} node{sorted.length === 1 ? "" : "s"}
        </div>
        {sorted.length === 0 ? (
          <p className="text-xs text-muted-foreground/60">
            No hypotheses yet.
          </p>
        ) : (
          <ul className="rounded-md border">
            {sorted.map((n) => (
              <IdeaRow
                key={n.nodeKey}
                node={n}
                baseline={run.baselineScore}
                direction={run.metricDirection}
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
