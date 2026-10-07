// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The project routes (/v1/workstreams), for the cards of a project's master and for the
// desktop's ProjectsSource: the rows, starting a proposed thread, and the project's stream.

import { type EventStreamLike, projectReopening, reopeningStream } from "@/lib/reopening-stream";
import type { ThreadRow } from "@/lib/projects";
import { type ThreadRowResponse, threadRowOf } from "@/lib/projects-wire";
import { FetchSseEventStream } from "@invergent/agent-chat-react";
import { parseError } from "./_errors";
import { authFetch } from "./auth";

/** The project's thread rows, or only *threadId*'s: none when it is not one of its live threads. */
export async function listThreads(projectId: string, threadId?: string): Promise<ThreadRow[]> {
  const query = threadId ? `?${new URLSearchParams({ thread_id: threadId })}` : "";
  const response = await authFetch(`/api/v1/workstreams/${projectId}/threads${query}`);
  if (!response.ok) return parseError(response, "Failed to fetch the project's threads");
  return ((await response.json()) as ThreadRowResponse[]).map(threadRowOf);
}

/** Start the thread a proposal's card *key* names. */
export async function startThread(projectId: string, proposalId: string, key: string): Promise<ThreadRow> {
  const response = await authFetch(`/api/v1/workstreams/${projectId}/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proposal_id: proposalId, key }),
  });
  if (!response.ok) return parseError(response, "The thread could not be started.");
  return threadRowOf((await response.json()) as ThreadRowResponse);
}

/**
 * The project's stream, ``ready`` then a ``change`` for each change. It opens itself again
 * after any failure, and ends, with ``onerror``, only when the project is gone: its route
 * answers 404 once the project is archived or is not the user's.
 */
export function openProjectStream(projectId: string): EventStreamLike<"ready" | "change"> {
  let gone = false;
  const fetchFn: typeof authFetch = async (input, init) => {
    const response = await authFetch(input, init);
    gone = response.status === 404;
    return response;
  };
  return reopeningStream<"ready" | "change">(
    () => new FetchSseEventStream(`/api/v1/workstreams/${projectId}/stream`, { fetchFn }),
    projectReopening(() => gone),
  );
}
