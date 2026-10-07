// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The cards of the workers a conversation started (desktop design, Section 12): a
// project's threads and a coordinator's workers, and the threads a project's master
// proposed for the user to start. Titles, goals, status lines and file names are
// written by a model, so each sits in its own <bdi>: a bidirectional control
// character in one cannot reorder the card around it.

import { useEffect, useRef, useState } from "react";
import { useAgentChatAdapterContext } from "../../adapter-context";
import type { AgentChatThreadProposal, AgentChatThreadRow, AgentChatWorker, ChatMessage } from "../../types";
import { Button } from "../ui/button";

const GROUP_LABEL: Record<AgentChatThreadRow["group"], string> = {
  waiting: "Waiting on you",
  working: "Working",
  idle: "Idle",
  resolved: "Resolved",
};
const STATE_LABEL: Record<AgentChatWorker["state"], string> = {
  working: "Working",
  reported: "Reported",
  failed: "Failed",
};
// A card names its first few files; the thread holds the rest.
const FILES_SHOWN = 3;

function firstLine(text: string | null): string | null {
  return text?.split("\n").find((line) => line.trim())?.trim() ?? null;
}

/** A report's first line that is not a markdown heading, without its bold or code marks. */
function statusLineOf(report: string | null): string | null {
  for (const line of report?.split("\n") ?? []) {
    if (/^\s*#{1,6}(\s|$)/.test(line)) continue;
    const plain = line.replace(/\*\*|__|`/g, "").trim();
    if (plain) return plain;
  }
  return null;
}

/** The card a "worker" or "thread_proposal" system message draws, or nothing. */
export function ThreadCards({ message }: { message: ChatMessage }) {
  if (message.worker) return <WorkerCard worker={message.worker} />;
  if (message.proposal) return <ProposalCard proposal={message.proposal} />;
  return null;
}

function WorkerCard({ worker }: { worker: AgentChatWorker }) {
  const { onFileSelect, onOpenSession, threadRows } = useAgentChatAdapterContext();
  // A project's thread shows its row, live; any other worker what its reports said.
  const live = threadRows?.[worker.id];
  const status = live
    ? GROUP_LABEL[live.group] + (live.progress ? ` · ${live.progress.done}/${live.progress.total}` : "")
    : STATE_LABEL[worker.state];
  const statusLine = live ? live.statusLine : statusLineOf(worker.report);
  const files = live ? live.files : worker.files;
  const name = worker.title ?? firstLine(worker.goal);
  const view = worker.title !== null ? "View thread" : "View worker";
  return (
    <div data-testid="worker-card" data-group={live?.group} className="my-2 rounded-lg border border-border px-3 py-2 text-sm">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-medium text-foreground" title={name ?? undefined}>
          <bdi>{name}</bdi>
        </span>
        <span data-testid="worker-card-status" className="shrink-0 text-xs text-muted-foreground">
          {status}
        </span>
      </div>
      {statusLine && (
        <p className="truncate text-foreground/70" title={statusLine}>
          <bdi>{statusLine}</bdi>
        </p>
      )}
      {files.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-x-3 text-xs">
          {files.slice(0, FILES_SHOWN).map((file) => (
            <li key={`${file.kind}:${file.ref}`} className="min-w-0 max-w-full truncate" title={file.label}>
              {file.kind === "file" && onFileSelect ? (
                <button
                  type="button"
                  className="min-w-0 max-w-full truncate underline"
                  onClick={() => onFileSelect(file.ref)}
                >
                  <bdi>{file.label}</bdi>
                </button>
              ) : (
                <bdi>{file.label}</bdi>
              )}
            </li>
          ))}
          {files.length > FILES_SHOWN && <li>+{files.length - FILES_SHOWN} more</li>}
        </ul>
      )}
      {onOpenSession && (
        <Button
          size="xs"
          variant="ghost"
          className="mt-1"
          aria-label={name ? `${view} ${name}` : undefined}
          onClick={() => onOpenSession(worker.id)}
        >
          {view}
        </Button>
      )}
    </div>
  );
}

function ProposalCard({ proposal }: { proposal: AgentChatThreadProposal }) {
  const { adapter, projectId, onOpenSession } = useAgentChatAdapterContext();
  // Started here: shown at once, before the thread's own event arrives.
  const [startedHere, setStartedHere] = useState<Record<string, string>>({});
  const [starting, setStarting] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const started = { ...startedHere, ...proposal.started };
  const canStart = !!projectId && !!adapter.startProposedThread;
  // A card started here gives its View thread the focus its Start took away with it,
  // unless the user has put the focus elsewhere meanwhile.
  const viewButtons = useRef<Record<string, HTMLButtonElement | null>>({});
  const [startedNow, setStartedNow] = useState<string | null>(null);
  useEffect(() => {
    if (startedNow === null) return;
    if (!document.activeElement || document.activeElement === document.body) viewButtons.current[startedNow]?.focus();
    setStartedNow(null);
  }, [startedNow]);

  const start = async (key: string) => {
    setStarting((current) => ({ ...current, [key]: true }));
    setErrors(({ [key]: _cleared, ...rest }) => rest);
    try {
      const row = await adapter.startProposedThread!({ projectId: projectId!, proposalId: proposal.proposalId, key });
      setStartedHere((current) => ({ ...current, [key]: row.id }));
      setStartedNow(key);
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [key]: error instanceof Error ? error.message : "The thread could not be started.",
      }));
    } finally {
      setStarting(({ [key]: _done, ...rest }) => rest);
    }
  };
  const startable = proposal.threads.filter(
    (thread) => thread.where === "cloud" && !started[thread.key] && !starting[thread.key],
  );
  // One at a time, each awaited: a card that fails leaves the rest to go on. Every
  // card waiting its turn counts as starting, so neither its Start nor Start all
  // starts it a second time.
  const startAll = async (keys: string[]) => {
    setStarting((current) => ({ ...current, ...Object.fromEntries(keys.map((key) => [key, true])) }));
    for (const key of keys) await start(key);
  };

  return (
    <div data-testid="thread-proposal-card" className="my-2 rounded-lg border border-border px-3 py-2 text-sm">
      <div className="flex items-center gap-2">
        <span className="flex-1 font-medium text-foreground">Proposed threads</span>
        {canStart && startable.length > 1 && (
          <Button size="xs" onClick={() => void startAll(startable.map((thread) => thread.key))}>
            Start all
          </Button>
        )}
      </div>
      <ul className="mt-1 space-y-2">
        {proposal.threads.map((thread) => {
          const threadId = started[thread.key];
          return (
            <li key={thread.key} data-testid="proposed-thread">
              <div className="break-words font-medium text-foreground">
                <bdi>{thread.title}</bdi>
              </div>
              <p className="line-clamp-2 break-words text-foreground/70">
                <bdi>{thread.goal}</bdi>
              </p>
              {threadId ? (
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span role="status">Started</span>
                  {onOpenSession && (
                    <Button
                      ref={(node) => {
                        viewButtons.current[thread.key] = node;
                      }}
                      size="xs"
                      variant="ghost"
                      aria-label={`View thread ${thread.title}`}
                      onClick={() => onOpenSession(threadId)}
                    >
                      View thread
                    </Button>
                  )}
                </span>
              ) : thread.where === "device" ? (
                <span className="text-xs text-muted-foreground">Works in a folder on your computer</span>
              ) : canStart ? (
                <Button
                  size="xs"
                  variant="outline"
                  disabled={!!starting[thread.key]}
                  aria-label={`${starting[thread.key] ? "Starting" : "Start"} ${thread.title}`}
                  onClick={() => void start(thread.key)}
                >
                  {starting[thread.key] ? "Starting…" : "Start"}
                </Button>
              ) : null}
              {/* A start refused once the card is started, here or elsewhere, is moot. */}
              {!threadId && errors[thread.key] && (
                <p role="alert" className="text-xs text-destructive">
                  {errors[thread.key]}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
