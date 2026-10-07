// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The project routes (/v1/workstreams) over the fetch they are given: the cards of a project's
// master call them, and in Surogate Desktop they are the ProjectsSource the page serves. Every
// import here is a file of web/src named with its extension, so a node test runs these routes
// over a fake fetch.

import type { ProjectsSource, ThreadRow } from "../lib/projects-contract";
import {
  type LibraryEntryResponse,
  type ProjectResponse,
  type ProjectSummaryResponse,
  type RoutineResponse,
  type ThreadRowResponse,
  libraryEntryOf,
  projectChangeOf,
  projectOf,
  projectSummaryOf,
  routineOf,
  threadRowOf,
} from "../lib/projects-wire.ts";
import { type EventStreamLike, projectStream } from "../lib/reopening-stream.ts";
import { parseError } from "./_errors.ts";

export type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** One connection to the server-sent events at *url*, over *fetchFn*. */
export type OpenEvents = (url: string, fetchFn: Fetch) => EventStreamLike<"ready" | "change">;

export interface WorkstreamRoutes extends ProjectsSource {
  /** Start the thread a proposal's card *key* names. */
  start(projectId: string, proposalId: string, key: string): Promise<ThreadRow>;
  /**
   * The project's stream, ``ready`` then a ``change`` for each change. It opens itself again
   * after any failure, and ends, with ``onerror``, only when the project is gone: its route
   * answers its own 404 once the project is archived or is not the user's.
   */
  stream(projectId: string): EventStreamLike<"ready" | "change">;
}

const ROUTE = "/api/v1/workstreams";

function sent(method: string, body?: unknown): RequestInit {
  if (body === undefined) return { method };
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// An id goes into a path as one segment, encoded: an id holding "/", "?" or "#" reaches no other
// route with the user's token. A dot segment the URL would resolve away is no id at all.
function segment(id: string, refusal: string): string {
  if (id === "." || id === "..") throw new Error(refusal);
  return encodeURIComponent(id);
}
const project = (id: string) => `/${segment(id, "No such project.")}`;
const thread = (id: string) => `/${segment(id, "No such thread.")}`;

/** The thread a change names, or null when it is project-wide or cannot be read. */
function changedThread(data: string): string | null {
  try {
    const named = (JSON.parse(data) as { thread_id?: unknown }).thread_id;
    return typeof named === "string" ? named : null;
  } catch {
    return null;
  }
}

export function workstreamRoutes(fetchFn: Fetch, openEvents: OpenEvents): WorkstreamRoutes {
  async function asked<T>(path: string, init: RequestInit | undefined, failure: string): Promise<T> {
    const response = await fetchFn(`${ROUTE}${path}`, init);
    if (!response.ok) return parseError(response, failure);
    return (response.status === 204 ? undefined : await response.json()) as T;
  }
  const row = async (path: string, init: RequestInit, failure: string) =>
    threadRowOf(await asked<ThreadRowResponse>(path, init, failure));

  const routes: WorkstreamRoutes = {
    list: async () =>
      (await asked<ProjectSummaryResponse[]>("", undefined, "Failed to fetch the projects")).map(projectSummaryOf),
    get: async (projectId) => projectOf(await asked<ProjectResponse>(project(projectId), undefined, "Failed to fetch the project")),
    create: async (input) => projectOf(await asked<ProjectResponse>("", sent("POST", input), "The project could not be created.")),
    update: async (projectId, change) =>
      projectOf(await asked<ProjectResponse>(project(projectId), sent("PATCH", projectChangeOf(change)), "The project could not be changed.")),
    archive: async (projectId) => asked<void>(project(projectId), sent("DELETE"), "The project could not be archived."),
    threads: async (projectId, threadId) => {
      const query = threadId ? `?${new URLSearchParams({ thread_id: threadId })}` : "";
      const rows = await asked<ThreadRowResponse[]>(`${project(projectId)}/threads${query}`, undefined, "Failed to fetch the project's threads");
      return rows.map(threadRowOf);
    },
    resolve: async (projectId, threadId) => row(`${project(projectId)}/threads${thread(threadId)}/resolve`, sent("POST"), "The thread could not be resolved."),
    reopen: async (projectId, threadId) => row(`${project(projectId)}/threads${thread(threadId)}/reopen`, sent("POST"), "The thread could not be reopened."),
    library: async (projectId) =>
      (await asked<LibraryEntryResponse[]>(`${project(projectId)}/library`, undefined, "Failed to fetch the project's Library")).map(libraryEntryOf),
    // The schedules the project's master made, of every status, as many as the shell takes.
    routines: async (projectId) => {
      const { masterSessionId } = await routes.get(projectId);
      const query = new URLSearchParams({ created_from_session_id: masterSessionId, status: "all", limit: "200" });
      const response = await fetchFn(`/api/v1/scheduled-work?${query}`);
      if (!response.ok) return parseError(response, "Failed to fetch the project's routines");
      return ((await response.json()) as { items: RoutineResponse[] }).items.map(routineOf);
    },
    start: async (projectId, proposalId, key) =>
      row(`${project(projectId)}/threads`, sent("POST", { proposal_id: proposalId, key }), "The thread could not be started."),
    stream: (projectId) => {
      const path = `${ROUTE}${project(projectId)}/stream`;
      return projectStream((watched) => openEvents(path, watched), fetchFn);
    },
    subscribe(projectId, onChange) {
      const events = routes.stream(projectId);
      // Ready again after a reconnect: whatever changed meanwhile is read again.
      events.addEventListener("ready", () => onChange(null));
      events.addEventListener("change", (event) => onChange(changedThread(event.data)));
      // The project is gone: read again, it is listed no more.
      events.onerror = () => onChange(null);
      return () => events.close();
    },
  };
  return routes;
}
