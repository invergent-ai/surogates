// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Projects and their thread rows as the routes answer them (/v1/workstreams), in snake_case,
// and the shell's types (projects-contract.d.ts) they map to, field by field. Pure, so the
// desktop's ProjectsSource maps the same way, and a test can run it.

import type {
  ChangedBy, FileVersion, LibraryEntry, ProducedFile, Project, ProjectsSource, ProjectSummary, Routine, ThreadPlace, ThreadRow,
  UndoResult,
} from "./projects-contract";

/** A thread's row as GET /v1/workstreams/{id}/threads answers it. */
export interface ThreadRowResponse {
  id: string;
  title: string;
  group: ThreadRow["group"];
  reason: ThreadRow["reason"];
  status_line: string | null;
  progress: { done: number; total: number } | null;
  // A server from before file history sends a file with no landing, and a later one may send a mark not known here.
  files: { kind: "file" | "artifact"; label: string; ref: string; thread_id: string; landing?: ProducedFile["landing"] }[];
  place: { kind: "cloud" } | { kind: "device"; device_id: string; device_name: string; online: boolean };
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

const MARKS: readonly unknown[] = ["landed", "redoing", "not_merged", "undone"];

/** A file's mark when it is one the page knows, else none: the file is served either way. */
function markOf(landing: unknown): NonNullable<ProducedFile["landing"]> | null {
  return MARKS.includes(landing) ? (landing as NonNullable<ProducedFile["landing"]>) : null;
}

function placeOf(place: ThreadRowResponse["place"]): ThreadPlace {
  return place.kind === "cloud"
    ? { kind: "cloud" }
    : { kind: "device", deviceId: place.device_id, deviceName: place.device_name, online: place.online };
}

export function threadRowOf(row: ThreadRowResponse): ThreadRow {
  return {
    id: row.id,
    title: row.title,
    group: row.group,
    reason: row.reason,
    statusLine: row.status_line,
    progress: row.progress,
    files: row.files.map(({ kind, label, ref, thread_id, landing }) => ({ kind, label, ref, threadId: thread_id, landing: markOf(landing) })),
    place: placeOf(row.place),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    resolvedAt: row.resolved_at,
  };
}

/** A project as GET /v1/workstreams lists it. */
export interface ProjectSummaryResponse {
  id: string;
  name: string;
  icon: string | null;
  created_at: string;
  updated_at: string;
  waiting: number;
  working: number;
}

/** A project as GET, POST and PATCH /v1/workstreams[/{id}] answer it. */
export interface ProjectResponse extends ProjectSummaryResponse {
  goal: string | null;
  instructions: string;
  master_session_id: string;
  coordinator_tier: Project["coordinatorTier"];
  thread_tier: Project["threadTier"];
}

export function projectSummaryOf(project: ProjectSummaryResponse): ProjectSummary {
  return {
    id: project.id,
    name: project.name,
    icon: project.icon,
    createdAt: project.created_at,
    updatedAt: project.updated_at,
    waiting: project.waiting,
    working: project.working,
  };
}

export function projectOf(project: ProjectResponse): Project {
  return {
    ...projectSummaryOf(project),
    goal: project.goal,
    instructions: project.instructions,
    masterSessionId: project.master_session_id,
    coordinatorTier: project.coordinator_tier,
    threadTier: project.thread_tier,
  };
}

export type ProjectChange = Parameters<ProjectsSource["update"]>[1];

const CHANGE_FIELDS: Record<keyof ProjectChange, string> = {
  name: "name",
  icon: "icon",
  goal: "goal",
  instructions: "instructions",
  coordinatorTier: "coordinator_tier",
  threadTier: "thread_tier",
};

/** A change as PATCH /v1/workstreams/{id} takes it: the fields it names, and no others. */
export function projectChangeOf(change: ProjectChange): Record<string, unknown> {
  return Object.fromEntries(Object.entries(change)
    .filter(([field]) => Object.hasOwn(CHANGE_FIELDS, field))
    .map(([field, value]) => [CHANGE_FIELDS[field as keyof ProjectChange], value]));
}

/** A file of a project's Library, as GET /v1/workstreams/{id}/library lists it. */
export interface LibraryEntryResponse {
  path: string;
  origin: LibraryEntry["origin"];
  thread_id: string | null;
  size: number | null;
  updated_at: string | null;
  place: ThreadRowResponse["place"];
}

export function libraryEntryOf(entry: LibraryEntryResponse): LibraryEntry {
  return {
    path: entry.path,
    origin: entry.origin,
    threadId: entry.thread_id,
    size: entry.size,
    updatedAt: entry.updated_at,
    place: placeOf(entry.place),
  };
}

/** A schedule as GET /v1/scheduled-work lists it, as far as a project's Routines show it. */
export interface RoutineResponse {
  id: string;
  name: string | null;
  schedule_display: string;
  next_run_at: string | null;
  status: string;
}

export function routineOf(routine: RoutineResponse): Routine {
  return {
    id: routine.id,
    // A schedule made without a name shows its schedule alone.
    name: routine.name ?? "",
    scheduleDisplay: routine.schedule_display,
    nextRunAt: routine.next_run_at,
    status: routine.status,
  };
}

/** A version of a file, as GET /v1/workstreams/{id}/history lists it. */
export interface FileVersionResponse {
  id: string;
  path: string;
  // you, a thread ({kind, thread_id, title}) or a routine ({kind, name}); a later server may name another kind.
  by: unknown;
  at: string;
  change: string;
  merged: boolean;
  available: boolean;
  landing_id: string | null;
}

const CHANGES: readonly FileVersion["change"][] = ["added", "changed", "deleted", "restored", "undone"];

// Who the route says changed a file. Someone this page has no name for is no one it can name:
// the version is still listed.
export function changedByOf(by: unknown): ChangedBy | null {
  const { kind, thread_id, title, name } = (typeof by === "object" && by !== null ? by : {}) as Record<string, unknown>;
  if (kind === "you") return { kind };
  if (kind === "thread" && typeof thread_id === "string" && typeof title === "string") return { kind, threadId: thread_id, title };
  if (kind === "routine" && typeof name === "string") return { kind, name };
  return null;
}

/** What POST /v1/workstreams/{id}/history/restore answers. */
export interface UndoResultResponse {
  applied: string[];
  skipped: { path: string; by: unknown }[];
  picked_up: string[];
}

const paths = (value: unknown): string[] => {
  if (!Array.isArray(value) || value.some((path) => typeof path !== "string")) throw new TypeError("Not a list of paths");
  return value.map((path: string) => path);
};

// What a Restore did, each list as this page knows it: one it cannot read refuses the answer, and
// someone it has no name for is no one it names.
export function undoResultOf(result: UndoResultResponse): UndoResult {
  if (!Array.isArray(result.skipped)) throw new TypeError("Not a list of files left as they were");
  return {
    applied: paths(result.applied),
    skipped: result.skipped.map((left) => {
      const { path, by } = (typeof left === "object" && left !== null ? left : {}) as Record<string, unknown>;
      if (typeof path !== "string") throw new TypeError("A file left as it was names no file");
      return { path, by: by === null ? null : changedByOf(by) };
    }),
    pickedUp: paths(result.picked_up),
  };
}

// A way of changing a file this page does not know is a change all the same; and what a version
// says beside who, when and how is taken as the plain case unless it says otherwise.
export function fileVersionOf(version: FileVersionResponse): FileVersion {
  return {
    id: version.id,
    path: version.path,
    by: changedByOf(version.by),
    at: version.at,
    change: CHANGES.find((known) => known === version.change) ?? "changed",
    merged: version.merged !== false,
    available: version.available !== false,
    landingId: typeof version.landing_id === "string" ? version.landing_id : null,
  };
}
