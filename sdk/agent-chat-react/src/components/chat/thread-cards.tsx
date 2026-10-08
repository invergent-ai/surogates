// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The cards of the workers a conversation started (desktop design, Section 12): a
// project's threads and a coordinator's workers, and the threads a project's master
// proposed for the user to start. Titles, goals, status lines and file names are
// written by a model, so each sits in its own <bdi>: a bidirectional control
// character in one cannot reorder the card around it.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { useAgentChatAdapterContext } from "../../adapter-context";
import type { AgentChatAdapter, AgentChatThreadProposal, AgentChatThreadRow, AgentChatWorker, ChatMessage } from "../../types";
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

// What each proposed card's Start did, by proposal and key, kept for the adapter outside the
// card: a change of view mode draws the card anew, and "Starting…", the error and the focus
// a start owes its View thread survive it. A start keeps the way it went, in the cloud or on
// the device, so that only that way's button says "Starting…".
type CardStart =
  | { state: "starting"; where: "device" | "cloud" }
  | { state: "failed"; error: string }
  | { state: "started"; threadId: string; focus: boolean };
const cardStarts = new WeakMap<AgentChatAdapter, Map<string, CardStart>>();
const startListeners = new Set<() => void>();
let startsChanged = 0;
// How many cards of each proposal are drawn now: a start that finishes while none is owes no focus,
// which would otherwise jump to its View thread whenever the card is drawn again.
const drawnCards = new Map<string, number>();

function setCardStart(adapter: AgentChatAdapter, card: string, start: CardStart): void {
  const starts = cardStarts.get(adapter) ?? new Map<string, CardStart>();
  cardStarts.set(adapter, starts.set(card, start));
  startsChanged++;
  for (const listener of startListeners) listener();
}

function useCardStarts(adapter: AgentChatAdapter): ReadonlyMap<string, CardStart> {
  useSyncExternalStore(
    (listener) => {
      startListeners.add(listener);
      return () => {
        startListeners.delete(listener);
      };
    },
    () => startsChanged,
  );
  return cardStarts.get(adapter) ?? new Map();
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
  const starts = useCardStarts(adapter);
  const startOf = (key: string) => starts.get(`${proposal.proposalId}:${key}`);
  const told = (key: string, start: CardStart) => setCardStart(adapter, `${proposal.proposalId}:${key}`, start);
  // Started here: shown at once, before the thread's own event arrives.
  const started: Record<string, string> = {};
  for (const { key } of proposal.threads) {
    const start = startOf(key);
    if (start?.state === "started") started[key] = start.threadId;
  }
  Object.assign(started, proposal.started);
  const startingAt = (key: string) => {
    const start = startOf(key);
    return start?.state === "starting" ? start.where : undefined;
  };
  const starting = (key: string) => startingAt(key) !== undefined;
  const canStart = !!projectId && !!adapter.startProposedThread;
  // Only Surogate Desktop starts a thread in a folder of the user's computer.
  const canStartHere = !!projectId && !!adapter.startLocalThread;
  // A card started here gives its View thread the focus its Start took away with it,
  // unless the user has put the focus elsewhere meanwhile; a card drawn anew owes it still,
  // when one was drawn as the start finished.
  const viewButtons = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => {
    drawnCards.set(proposal.proposalId, (drawnCards.get(proposal.proposalId) ?? 0) + 1);
    return () => {
      const left = (drawnCards.get(proposal.proposalId) ?? 1) - 1;
      if (left > 0) drawnCards.set(proposal.proposalId, left);
      else drawnCards.delete(proposal.proposalId);
    };
  }, [proposal.proposalId]);
  useEffect(() => {
    for (const { key } of proposal.threads) {
      const start = startOf(key);
      if (start?.state !== "started" || !start.focus) continue;
      if (!document.activeElement || document.activeElement === document.body) viewButtons.current[key]?.focus();
      told(key, { ...start, focus: false });
    }
  });

  // In the cloud, or with *title*, in a folder of this computer.
  const start = async (key: string, title?: string) => {
    told(key, { state: "starting", where: title === undefined ? "cloud" : "device" });
    try {
      const card = { projectId: projectId!, proposalId: proposal.proposalId, key };
      const row = title === undefined
        ? await adapter.startProposedThread!(card)
        : await adapter.startLocalThread!({ ...card, title });
      told(key, { state: "started", threadId: row.id, focus: drawnCards.has(proposal.proposalId) });
    } catch (error) {
      told(key, { state: "failed", error: error instanceof Error ? error.message : "The thread could not be started." });
    }
  };
  const startable = proposal.threads.filter(
    (thread) => thread.where === "cloud" && !started[thread.key] && !starting(thread.key),
  );
  // One at a time, each awaited: a card that fails leaves the rest to go on. Every
  // card waiting its turn counts as starting, so neither its Start nor Start all
  // starts it a second time.
  const startAll = async (keys: string[]) => {
    for (const key of keys) told(key, { state: "starting", where: "cloud" });
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
          const tried = startOf(thread.key);
          return (
            <li key={thread.key} data-testid="proposed-thread">
              <div className="break-words font-medium text-foreground">
                <bdi>{thread.title}</bdi>
              </div>
              <p className="line-clamp-2 break-words text-foreground/70">
                <bdi>{thread.goal}</bdi>
              </p>
              <span className="flex items-center gap-2 text-xs text-muted-foreground">
                {/* There before it says Started, so that a screen reader hears it say so. */}
                <span role="status">{threadId ? "Started" : ""}</span>
                {threadId ? (
                  onOpenSession && (
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
                  )
                ) : thread.where === "device" ? (
                  <>
                    <span>
                      {canStartHere
                        ? "Allow Surogate to work in a folder on your device"
                        : "This works in a folder on your device, which needs Surogate Desktop"}
                    </span>
                    {canStartHere && (
                      <Button
                        size="xs"
                        variant="outline"
                        disabled={starting(thread.key)}
                        aria-label={`${startingAt(thread.key) === "device" ? "Starting" : "Allow"} ${thread.title}`}
                        onClick={() => void start(thread.key, thread.title)}
                      >
                        {startingAt(thread.key) === "device" ? "Starting…" : "Allow"}
                      </Button>
                    )}
                    {canStart && (
                      <Button
                        size="xs"
                        variant="ghost"
                        disabled={starting(thread.key)}
                        aria-label={
                          startingAt(thread.key) === "cloud"
                            ? `Starting ${thread.title}`
                            : `Run ${thread.title} in the cloud instead`
                        }
                        onClick={() => void start(thread.key)}
                      >
                        {startingAt(thread.key) === "cloud" ? "Starting…" : "Run in the cloud instead"}
                      </Button>
                    )}
                  </>
                ) : canStart ? (
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={starting(thread.key)}
                    aria-label={`${starting(thread.key) ? "Starting" : "Start"} ${thread.title}`}
                    onClick={() => void start(thread.key)}
                  >
                    {starting(thread.key) ? "Starting…" : "Start"}
                  </Button>
                ) : null}
              </span>
              {/* A start refused once the card is started, here or elsewhere, is moot. */}
              {!threadId && tried?.state === "failed" && (
                <p role="alert" className="text-xs text-destructive">
                  {tried.error}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
