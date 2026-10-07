// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The project routes (/v1/workstreams) over the fetch they are given: the cards of a project's
// master call them, and in Surogate Desktop they are the ProjectsSource the page serves. Every
// import here is a file of web/src named with its extension, so a node test runs these routes
// over a fake fetch.

import type { ProjectsSource, ThreadRow } from "../lib/projects-contract";
import {
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

// A route's answer read in its shape, or refused: an object whose named fields are strings, mapped,
// or a list of them. Whatever else the body holds fails its mapping, which is refused the same way.
const one = <T>(fields: string[], map: (body: never) => T) => (body: unknown): T => {
  const record = (body ?? {}) as Record<string, unknown>;
  if (typeof body !== "object" || Array.isArray(body) || fields.some((field) => typeof record[field] !== "string")) throw new TypeError("Not the route's shape");
  return map(body as never);
};
const many = <T>(item: (body: unknown) => T) => (body: unknown): T[] => {
  if (!Array.isArray(body)) throw new TypeError("Not a list");
  return body.map(item);
};
const SUMMARY = one(["id", "name", "created_at", "updated_at"], projectSummaryOf);
const PROJECT = one(["id", "name", "created_at", "updated_at", "master_session_id"], projectOf);
const ROW = one(["id", "title", "group", "created_at", "updated_at"], threadRowOf);
const ENTRY = one(["path", "origin"], libraryEntryOf);
const ROUTINE = one(["id", "schedule_display", "status"], routineOf);
const ROUTINES = (body: unknown) => many(ROUTINE)((body as { items?: unknown } | null)?.items);

export function workstreamRoutes(fetchFn: Fetch, openEvents: OpenEvents): WorkstreamRoutes {
  // A body that is no JSON, or not the route's shape, is the route's own failure: never an
  // engine's words, and never a success with nothing in it.
  async function read<T>(url: string, init: RequestInit | undefined, failure: string, shape: (body: unknown) => T): Promise<T> {
    const response = await fetchFn(url, init);
    if (!response.ok) return parseError(response, failure);
    try {
      return shape(response.status === 204 ? undefined : await response.json());
    } catch {
      throw new Error(failure);
    }
  }
  const asked = <T>(path: string, init: RequestInit | undefined, failure: string, shape: (body: unknown) => T) =>
    read(`${ROUTE}${path}`, init, failure, shape);
  const row = (path: string, init: RequestInit, failure: string) => asked(path, init, failure, ROW);

  const routes: WorkstreamRoutes = {
    list: () => asked("", undefined, "Failed to fetch the projects", many(SUMMARY)),
    get: async (projectId) => asked(project(projectId), undefined, "Failed to fetch the project", PROJECT),
    create: (input) => asked("", sent("POST", input), "The project could not be created.", PROJECT),
    update: async (projectId, change) =>
      asked(project(projectId), sent("PATCH", projectChangeOf(change)), "The project could not be changed.", PROJECT),
    // Archived, the route answers nothing: whatever body it sends is not read.
    archive: async (projectId) => asked(project(projectId), sent("DELETE"), "The project could not be archived.", () => undefined),
    threads: async (projectId, threadId) => {
      const query = threadId ? `?${new URLSearchParams({ thread_id: threadId })}` : "";
      return asked(`${project(projectId)}/threads${query}`, undefined, "Failed to fetch the project's threads", many(ROW));
    },
    resolve: async (projectId, threadId) => row(`${project(projectId)}/threads${thread(threadId)}/resolve`, sent("POST"), "The thread could not be resolved."),
    reopen: async (projectId, threadId) => row(`${project(projectId)}/threads${thread(threadId)}/reopen`, sent("POST"), "The thread could not be reopened."),
    library: async (projectId) => asked(`${project(projectId)}/library`, undefined, "Failed to fetch the project's Library", many(ENTRY)),
    // The schedules the project's master made, of every status, as many as the shell takes.
    routines: async (projectId) => {
      const { masterSessionId } = await routes.get(projectId);
      const query = new URLSearchParams({ created_from_session_id: masterSessionId, status: "all", limit: "200" });
      return read(`/api/v1/scheduled-work?${query}`, undefined, "Failed to fetch the project's routines", ROUTINES);
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
