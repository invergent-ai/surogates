// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Metadata tab — last judge verdict as a toned callout, then the
// identifiers table. Description and rubric live in the hero card and
// are intentionally not repeated here.
import { Fragment } from "react";

import type { AgentChatMissionSummary } from "../../types";


export interface MissionMetadataTabProps {
  mission: AgentChatMissionSummary;
}


function verdictToneClass(result: string): string {
  if (result === "satisfied") {
    return "border-emerald-500/30 bg-emerald-500/5 text-emerald-700 dark:text-emerald-400";
  }
  if (result === "failed" || result === "blocked") {
    return "border-destructive/30 bg-destructive/5 text-destructive";
  }
  return "border-amber-500/30 bg-amber-500/5 text-amber-700 dark:text-amber-400";
}


export function MissionMetadataTab({ mission }: MissionMetadataTabProps) {
  type Row = { label: string; value: React.ReactNode };
  const rows: Row[] = [
    {
      label: "Mission ID",
      value: <code className="text-[11px]">{mission.id}</code>,
    },
    {
      label: "Session ID",
      value: <code className="text-[11px]">{mission.sessionId}</code>,
    },
    {
      label: "Agent ID",
      value: <code className="text-[11px]">{mission.agentId}</code>,
    },
    { label: "Status", value: mission.status },
    {
      label: "Iteration",
      value: `${mission.iteration} / ${mission.maxIterations}`,
    },
    {
      label: "Owner",
      value: mission.userId ? (
        <>
          user <code className="text-[11px]">{mission.userId}</code>
        </>
      ) : mission.serviceAccountId ? (
        <>
          service account{" "}
          <code className="text-[11px]">{mission.serviceAccountId}</code>
        </>
      ) : (
        "—"
      ),
    },
    {
      label: "Created",
      value: (
        <span className="font-mono text-[11px]">
          {new Date(mission.createdAt).toLocaleString()}
        </span>
      ),
    },
    {
      label: "Updated",
      value: (
        <span className="font-mono text-[11px]">
          {new Date(mission.updatedAt).toLocaleString()}
        </span>
      ),
    },
  ];
  if (mission.pausedReason) {
    rows.push({ label: "Paused reason", value: mission.pausedReason });
  }
  if (mission.cancelledReason) {
    rows.push({ label: "Cancelled reason", value: mission.cancelledReason });
  }

  return (
    <div className="space-y-6">
      {mission.lastEvaluationResult ? (
        <section
          className={`space-y-2 rounded-md border p-4 ${verdictToneClass(mission.lastEvaluationResult)}`}
        >
          <div className="font-mono text-[10px] uppercase tracking-widest">
            Last verdict — {mission.lastEvaluationResult}
          </div>
          {mission.lastEvaluationFeedback ? (
            <p className="whitespace-pre-wrap text-sm font-medium text-foreground/90">
              {mission.lastEvaluationFeedback}
            </p>
          ) : null}
          {mission.lastEvaluationExplanation ? (
            <p className="whitespace-pre-wrap text-xs text-muted-foreground/80">
              {mission.lastEvaluationExplanation}
            </p>
          ) : null}
          {mission.lastEvaluationAt ? (
            <p className="font-mono text-[10px] text-muted-foreground/60">
              {new Date(mission.lastEvaluationAt).toLocaleString()}
            </p>
          ) : null}
        </section>
      ) : null}
      <section>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground/70">
          Identifiers
        </div>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
          {rows.map((r) => (
            <Fragment key={r.label}>
              <dt className="text-muted-foreground/70">{r.label}</dt>
              <dd className="break-all text-foreground/90">{r.value}</dd>
            </Fragment>
          ))}
        </dl>
      </section>
    </div>
  );
}
