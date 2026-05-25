// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// TurnSummaryCard — Simple-mode per-turn recap card rendered below
// the final assistant text. Lists artifacts the harness's turn
// summarizer judged notable; each artifact dispatches to the right
// existing viewer (file viewer, ArtifactBlock, external link, terminal
// detail dialog).

import { ArtifactBlock } from "./artifacts/artifact-block";
import { WorkspaceFileCard } from "./workspace-file-card";
import { Shimmer } from "../ai-elements/shimmer";
import { Skeleton } from "../ui/skeleton";
import type {
  AgentChatTurnArtifactRef,
  AgentChatTurnSummary,
  ChatMessage,
} from "../../types";
import type { ArtifactKind } from "../../types";

export interface TurnSummaryCardProps {
  summary: AgentChatTurnSummary;
  /** Required to mount ``ArtifactBlock`` for ``kind: "artifact"`` refs. */
  sessionId: string | null;
  /** Used to resolve ``kind: "artifact"`` refs back to their
   *  ``artifact.created`` system-message metadata. */
  messages: ChatMessage[];
  /** Open a workspace file. Wired from the chat thread. */
  onFileSelect?: (path: string) => void;
  /** Open a terminal tool-call's detail dialog. When omitted, command
   *  artifacts render as plain text. */
  onCommandSelect?: (toolCallId: string) => void;
}

interface ResolvedArtifact {
  artifactId: string;
  name: string;
  kind: ArtifactKind;
  version: number;
}

function resolveArtifactRef(
  ref: string,
  messages: ChatMessage[],
): ResolvedArtifact | null {
  for (const msg of messages) {
    if (msg.role !== "system" || msg.systemKind !== "artifact") continue;
    const meta = msg.systemMeta ?? {};
    if (meta.artifact_id !== ref) continue;
    const kind = (meta.kind as ArtifactKind | undefined) ?? "markdown";
    const version = typeof meta.version === "number" ? meta.version : 1;
    const name = typeof meta.name === "string" ? meta.name : "";
    return { artifactId: ref, name, kind, version };
  }
  return null;
}

export function TurnSummaryCard({
  summary,
  sessionId,
  messages,
  onFileSelect,
  onCommandSelect,
}: TurnSummaryCardProps) {
  const hasRecap = summary.recap.trim().length > 0;
  const hasArtifacts = summary.artifacts.length > 0;
  if (!hasRecap && !hasArtifacts) return null;

  // Split artifacts so file/artifact refs render as full-width rich
  // cards (Claude-style download card + ArtifactBlock) while
  // url/command refs stay in the bullet list. Mixed turns get both
  // sections in source order.
  return (
    <div className="mt-3 rounded border border-border bg-muted/50 px-3 py-2 text-sm">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide">
        Summary
      </div>
      {hasRecap && (
        <p className="mb-2 whitespace-pre-wrap text-foreground">
          {summary.recap}
        </p>
      )}
      {hasArtifacts && (
        <div className="space-y-2">
          {summary.artifacts.map((artifact, i) => {
            const key = `${artifact.kind}:${artifact.ref}:${i}`;
            if (artifact.kind === "file" && sessionId) {
              return (
                <WorkspaceFileCard
                  key={key}
                  sessionId={sessionId}
                  path={artifact.ref}
                  label={artifact.label}
                />
              );
            }
            if (artifact.kind === "artifact") {
              const resolved = sessionId
                ? resolveArtifactRef(artifact.ref, messages)
                : null;
              if (resolved && sessionId) {
                return (
                  <ArtifactBlock
                    key={key}
                    sessionId={sessionId}
                    artifactId={resolved.artifactId}
                    name={resolved.name || artifact.label}
                    kind={resolved.kind}
                    version={resolved.version}
                  />
                );
              }
            }
            return (
              <div
                key={key}
                className="flex items-baseline gap-2 text-sm"
              >
                <span className="text-muted-foreground" aria-hidden>
                  •
                </span>
                <ArtifactRow
                  artifact={artifact}
                  sessionId={sessionId}
                  messages={messages}
                  onFileSelect={onFileSelect}
                  onCommandSelect={onCommandSelect}
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * Placeholder shown between the agent's final answer and the
 * TurnSummaryCard arriving. The harness's turn summarizer runs an
 * LLM call after the last assistant response and before
 * ``session.complete``; without an indicator users see their answer
 * appear and then a Summary card pop in seconds later with no
 * explanation. This component fills that gap with the same outer
 * frame as the eventual card so the layout doesn't jump when the
 * real summary lands.
 */
export function TurnSummaryPending() {
  return (
    <div className="mt-3 rounded border border-border bg-muted/50 px-3 py-2 text-sm">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide">
        Summary
      </div>
      <Shimmer duration={3} spread={3} className="mb-2 text-sm">
        Summarizing conversation...
      </Shimmer>
      <div className="space-y-1.5">
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-3 w-2/3" />
      </div>
    </div>
  );
}

interface ArtifactRowProps {
  artifact: AgentChatTurnArtifactRef;
  sessionId: string | null;
  messages: ChatMessage[];
  onFileSelect?: (path: string) => void;
  onCommandSelect?: (toolCallId: string) => void;
}

function ArtifactRow({
  artifact,
  sessionId,
  messages,
  onFileSelect,
  onCommandSelect,
}: ArtifactRowProps) {
  if (artifact.kind === "file") {
    if (!onFileSelect) {
      return (
        <span className="truncate text-muted-foreground">
          {artifact.label}
        </span>
      );
    }
    return (
      <button
        type="button"
        onClick={() => onFileSelect(artifact.ref)}
        className="truncate cursor-pointer text-primary hover:underline"
      >
        {artifact.label}
      </button>
    );
  }

  if (artifact.kind === "url") {
    return (
      <a
        href={artifact.ref}
        target="_blank"
        rel="noopener noreferrer"
        className="truncate text-primary hover:underline"
      >
        {artifact.label}
      </a>
    );
  }

  if (artifact.kind === "command") {
    if (!onCommandSelect) {
      return (
        <span className="truncate text-muted-foreground">
          {artifact.label}
        </span>
      );
    }
    return (
      <button
        type="button"
        onClick={() => onCommandSelect(artifact.ref)}
        className="truncate text-left cursor-pointer text-primary hover:underline"
      >
        {artifact.label}
      </button>
    );
  }

  // kind === "artifact"
  const resolved = sessionId ? resolveArtifactRef(artifact.ref, messages) : null;
  if (!resolved) {
    // No matching artifact.created event yet (truncated history, or
    // the summarizer cited a stale reference). Fall back to plain
    // text so the card stays informative.
    return (
      <span className="truncate text-muted-foreground">
        {artifact.label}
      </span>
    );
  }
  return (
    <div className="flex-1 min-w-0">
      <ArtifactBlock
        sessionId={sessionId!}
        artifactId={resolved.artifactId}
        name={resolved.name || artifact.label}
        kind={resolved.kind}
        version={resolved.version}
      />
    </div>
  );
}
