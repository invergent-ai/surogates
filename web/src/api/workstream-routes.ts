// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The project routes (/v1/workstreams) over the fetch they are given, and the list of the chats in no project: the cards
// of a project's master call them, and in Surogate Desktop they are the ProjectsSource the page serves. Every
// import here is a file of web/src named with its extension, so a node test runs these routes
// over a fake fetch.

import type { DeletedFiles, ProjectsSource, RecentChat, ThreadRow } from "../lib/projects-contract";
import {
  fileVersionOf,
  libraryEntryOf,
  projectChangeOf,
  projectOf,
  projectSummaryOf,
  routineOf,
  threadRowOf,
  undoResultOf,
} from "../lib/projects-wire.ts";
import { type EventStreamLike, projectStream } from "../lib/reopening-stream.ts";
import { savedName } from "../lib/save-file.ts";
import { parseError } from "./_errors.ts";

export type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** One connection to the server-sent events at *url*, over *fetchFn*. */
export type OpenEvents = (url: string, fetchFn: Fetch) => EventStreamLike<"ready" | "change">;

/** Hand *data* to the user to save as *name*: a version of a file, opened. */
export type SaveFile = (data: Blob, name: string) => void;

export interface WorkstreamRoutes extends ProjectsSource {
  /** Start the thread a proposal's card *key* names, in the cloud. */
  start(projectId: string, proposalId: string, key: string): Promise<ThreadRow>;
  /**
   * Make the thread a proposal's card *key* names in a folder its user confirmed on their
   * computer: its id. It begins once that computer has bound it.
   */
  makeOnComputer(
    projectId: string, proposalId: string, key: string,
    execution: { kind: "device"; device_id: string; folder: string; nonce: string },
  ): Promise<string>;
  /** Begin a thread made on the user's computer, once that computer has bound it. */
  begin(projectId: string, threadId: string): Promise<ThreadRow>;
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
// A row's group is no part of its shape: one that is none, or no group's name, is a group the page does not know.
const ROW = one(["id", "title", "created_at", "updated_at"], threadRowOf);
const MADE = one(["thread_id"], (body: { thread_id: string }) => body.thread_id);
const ENTRY = one(["path", "origin"], libraryEntryOf);
const ROUTINE = one(["id", "schedule_display", "status"], routineOf);
const VERSION = one(["id", "path", "at"], fileVersionOf);
// GET …/history/deleted answers {files, more}. Whether there are more is a plain yes, or it is no.
const DELETED = one([], (body: { files?: unknown; more?: unknown }): DeletedFiles => ({ files: many(VERSION)(body.files), more: body.more === true }));
const ROUTINES = (body: unknown) => many(ROUTINE)((body as { items?: unknown } | null)?.items);
const PUT_BACK = one([], undoResultOf);

// A time of the server's as ISO 8601 in UTC with its Z: one that names no zone is the server's UTC, never read as this
// computer's local time.
function utc(at: string): string {
  const moment = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`);
  if (Number.isNaN(moment)) throw new TypeError("Not a time");
  return new Date(moment).toISOString();
}

// The computer a chat on a folder of the user's computer works on, by the name the server stamped (the chat SDK's
// sessionComputer reads it the same way); null for a chat in the cloud.
function computerOf(config: unknown): string | null {
  const execution = (config as { execution?: { kind?: unknown; device_name?: unknown } } | null)?.execution;
  return execution?.kind === "device" && typeof execution.device_name === "string" ? execution.device_name : null;
}

// GET /api/v1/sessions answers {sessions}: each a chat the user started, newest first, a project's own conversation and
// any chat under another never among them.
const CHAT = one(["id", "updated_at"], (body: { id: string; title?: unknown; updated_at: string; parent_id?: unknown; config?: unknown }): RecentChat | null =>
  body.parent_id == null
    ? { id: body.id, title: typeof body.title === "string" && body.title !== "" ? body.title : null, updatedAt: utc(body.updated_at), computer: computerOf(body.config) }
    : null);
const CHATS = (body: unknown) => many(CHAT)((body as { sessions?: unknown } | null)?.sessions).filter((chat): chat is RecentChat => chat !== null);

// An Undo whose answer never came back whole: the network cut it, or a gateway stopped waiting for the agent, which
// goes on with it all the same once it has it.
const UNDO_UNANSWERED = "The connection was lost before the agent answered: the undo may still finish.";
const GATEWAY_GAVE_UP = [502, 504];

export function workstreamRoutes(fetchFn: Fetch, openEvents: OpenEvents, saveFile: SaveFile): WorkstreamRoutes {
  // A body that is no JSON, or not the route's shape, is the route's own failure: never an
  // engine's words, and never a success with nothing in it. With *unanswered*, what is said of
  // a request whose answer never came back whole, its answer cut on its way or a gateway's.
  async function read<T>(
    url: string, init: RequestInit | undefined, failure: string, shape: (body: unknown) => T, unanswered?: string,
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetchFn(url, init);
    } catch (error) {
      if (unanswered === undefined) throw error;
      throw new Error(unanswered);
    }
    if (!response.ok) return parseError(response, unanswered !== undefined && GATEWAY_GAVE_UP.includes(response.status) ? unanswered : failure);
    const body = response.status === 204 ? undefined : await response.text().catch(() => {
      throw new Error(unanswered ?? failure);
    });
    try {
      return shape(body === undefined ? undefined : JSON.parse(body));
    } catch {
      throw new Error(failure);
    }
  }
  const asked = <T>(path: string, init: RequestInit | undefined, failure: string, shape: (body: unknown) => T, unanswered?: string) =>
    read(`${ROUTE}${path}`, init, failure, shape, unanswered);
  const row = (path: string, init: RequestInit, failure: string) => asked(path, init, failure, ROW);

  const routes: WorkstreamRoutes = {
    list: () => asked("", undefined, "Failed to fetch the projects", many(SUMMARY)),
    get: async (projectId) => asked(project(projectId), undefined, "Failed to fetch the project", PROJECT),
    create: (input) => asked("", sent("POST", input), "The project could not be created.", PROJECT),
    update: async (projectId, change) =>
      asked(project(projectId), sent("PATCH", projectChangeOf(change)), "The project could not be changed.", PROJECT),
    useComputer: async (projectId, execution) =>
      asked(`${project(projectId)}/computer`, sent("POST", { execution }), "This computer could not be used for the project.", PROJECT),
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
    history: async (projectId, path, place) => {
      const query = new URLSearchParams({ path, ...(place.kind === "device" ? { device_id: place.deviceId } : {}) });
      return asked(`${project(projectId)}/history?${query}`, undefined, "Failed to fetch the file's History", many(VERSION));
    },
    // The version's bytes, read whole before any of them is saved, and saved as data under the file's
    // own name: a page or a drawing among the project's files is never one this page's origin shows.
    openVersion: async (projectId, { versionId, path }) => {
      const failure = "The version could not be opened.";
      const address = `${ROUTE}${project(projectId)}/history/${segment(versionId, "No such version.")}/file?${new URLSearchParams({ path })}`;
      const response = await fetchFn(address);
      if (!response.ok) return parseError(response, failure);
      const held = await response.blob().catch(() => {
        throw new Error(failure);
      });
      saveFile(new Blob([held], { type: "application/octet-stream" }), savedName(path));
    },
    // The version and its file go in the body, as they are: neither is any part of the address.
    restore: async (projectId, { versionId, path }) =>
      asked(`${project(projectId)}/history/restore`, sent("POST", { version: versionId, path }), "The version could not be restored.", PUT_BACK),
    // The landing, or the thread, goes in the body, as it is.
    undo: async (projectId, target) => {
      const body = "landingId" in target ? { landing: target.landingId } : { thread: target.threadId };
      return asked(`${project(projectId)}/history/undo`, sent("POST", body), "The change could not be undone.", PUT_BACK, UNDO_UNANSWERED);
    },
    deleted: async (projectId) =>
      asked(`${project(projectId)}/history/deleted`, undefined, "Failed to fetch the project's deleted files", DELETED),
    // The agent's chats in no project, from the sessions route, which lists the chats the user started: one more than
    // asked, which says whether more come after the page.
    chats: async ({ offset, limit }) => {
      const page = new URLSearchParams({ limit: String(limit + 1), offset: String(offset) });
      const found = await read(`/api/v1/sessions?${page}`, undefined, "Failed to fetch the chats", CHATS);
      return { chats: found.slice(0, limit), more: found.length > limit };
    },
    start: async (projectId, proposalId, key) =>
      row(`${project(projectId)}/threads`, sent("POST", { proposal_id: proposalId, key }), "The thread could not be started."),
    makeOnComputer: async (projectId, proposalId, key, execution) =>
      asked(`${project(projectId)}/threads`, sent("POST", { proposal_id: proposalId, key, execution }), "The thread could not be started.", MADE),
    begin: async (projectId, threadId) =>
      row(`${project(projectId)}/threads${thread(threadId)}/start`, sent("POST"), "The thread could not be started."),
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
