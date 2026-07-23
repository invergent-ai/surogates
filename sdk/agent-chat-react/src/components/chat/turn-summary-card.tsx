// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// TurnSummaryCard — Simple-mode per-turn recap card rendered below
// the final assistant text. Lists artifacts the harness's turn
// summarizer judged notable. Structured artifacts already render live
// via their ``artifact.created`` marker in the thread, so the card
// links back to that block (scroll-into-view) instead of mounting a
// second ArtifactBlock — a duplicate mount replayed html/svg
// animations from zero and doubled the payload fetch.

import { FileTextIcon } from "lucide-react";
import { KIND_LABEL } from "./artifacts/artifact-block";
import { WorkspaceFileCard } from "./workspace-file-card";
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

function scrollToArtifactBlock(artifactId: string): void {
  if (typeof document === "undefined") return;
  const selector =
    typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? `[data-artifact-anchor="${CSS.escape(artifactId)}"]`
      : `[data-artifact-anchor="${artifactId}"]`;
  document
    .querySelector(selector)
    ?.scrollIntoView({ behavior: "smooth", block: "center" });
}

/**
 * Compact reference to an artifact that is already live elsewhere in
 * the thread; clicking scrolls the existing block into view.
 */
function ArtifactRefCard({
  artifact,
}: {
  artifact: ResolvedArtifact & { label: string };
}) {
  return (
    <button
      type="button"
      onClick={() => scrollToArtifactBlock(artifact.artifactId)}
      className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded border border-border bg-background px-3 py-2 text-left text-sm transition-colors hover:bg-muted/60"
    >
      <FileTextIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span className="min-w-0 flex-1 truncate font-medium text-foreground">
        {artifact.name || artifact.label}
      </span>
      <span className="shrink-0 text-xs text-muted-foreground">
        {KIND_LABEL[artifact.kind]}
      </span>
    </button>
  );
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
  if (!hasArtifacts) return null;

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
              if (resolved) {
                return (
                  <ArtifactRefCard
                    key={key}
                    artifact={{ ...resolved, label: artifact.label }}
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

interface ArtifactRowProps {
  artifact: AgentChatTurnArtifactRef;
  onFileSelect?: (path: string) => void;
  onCommandSelect?: (toolCallId: string) => void;
}

function ArtifactRow({
  artifact,
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

  // kind === "artifact" — only reached when the full-width branch
  // above could not resolve the ref (truncated history, or the
  // summarizer cited a stale reference): fall back to plain text so
  // the card stays informative.
  return (
    <span className="truncate text-muted-foreground">
      {artifact.label}
    </span>
  );
}
