// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A project's thread rows as the routes answer them (/v1/workstreams/{id}/threads), in
// snake_case, and the shell's ThreadRow (projects-contract.d.ts) they map to, field by field.
// Pure, so the desktop's ProjectsSource maps the same way, and a test can run it.

import type { ThreadRow } from "./projects-contract";

/** A thread's row as GET /v1/workstreams/{id}/threads answers it. */
export interface ThreadRowResponse {
  id: string;
  title: string;
  group: ThreadRow["group"];
  reason: ThreadRow["reason"];
  status_line: string | null;
  progress: { done: number; total: number } | null;
  files: { kind: "file" | "artifact"; label: string; ref: string; thread_id: string }[];
  place: { kind: "cloud" } | { kind: "device"; device_id: string; device_name: string; online: boolean };
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export function threadRowOf(row: ThreadRowResponse): ThreadRow {
  return {
    id: row.id,
    title: row.title,
    group: row.group,
    reason: row.reason,
    statusLine: row.status_line,
    progress: row.progress,
    files: row.files.map(({ kind, label, ref, thread_id }) => ({ kind, label, ref, threadId: thread_id })),
    place: row.place.kind === "cloud"
      ? { kind: "cloud" }
      : { kind: "device", deviceId: row.place.device_id, deviceName: row.place.device_name, online: row.place.online },
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    resolvedAt: row.resolved_at,
  };
}
