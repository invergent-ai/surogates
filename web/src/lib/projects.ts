// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Projects and their threads (desktop design, Section 12): the wire types, and fixtures
// with a thread in every group and with every reason, for fakes of a ProjectsSource.

import type { DeletedFiles, FileVersion, LibraryEntry, ProducedFile, Project, Routine, ThreadRow } from "./projects-contract.js";

export type * from "./projects-contract.js";

export interface ProjectFixtures {
  projects: Project[];
  threads: Record<string, ThreadRow[]>;
  library: Record<string, LibraryEntry[]>;
  routines: Record<string, Routine[]>;
  history: Record<string, Record<string, FileVersion[]>>; // by project, then by file
  deleted: Record<string, DeletedFiles>; // by project: its files that are gone, each the version that deleted it
}

export const FIXTURE_IDS = {
  report: "0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f",
  budget: "1c7f4d2f-9b3e-4d6f-8a21-2b3c4d5e6f70",
  question: "4fac7a5c-ce6b-4a9c-9d54-5e6f708192a3",
  approval: "5abd8b6d-df7c-4bad-8e65-6f708192a3b4",
  failed: "6bce9c7e-e08d-4cbe-9f76-708192a3b4c5",
  working: "2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081",
  computer: "7cdfad8f-f19e-4dcf-8a87-8192a3b4c5d6",
  idle: "3e9b6f4b-bd5a-4f8b-8c43-4d5e6f708192",
  resolved: "8de0be90-02af-4ed0-9b98-92a3b4c5d6e7",
} as const;

/** Two projects: a report with a thread in every group and with every reason, and a budget with routines only. Times count back from *now*. */
export function projectFixtures(now = Date.now()): ProjectFixtures {
  const ago = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
  const ids = FIXTURE_IDS;
  const cloud = { kind: "cloud" } as const;
  const thread = (id: string, title: string, minutes: number, rest: Partial<ThreadRow>): ThreadRow => ({
    id, title, group: "idle", reason: null, statusLine: null, progress: null, files: [], place: cloud,
    createdAt: ago(minutes + 60), updatedAt: ago(minutes), resolvedAt: null, ...rest,
  });
  // A file of each mark among them; an artifact has none.
  const file = (threadId: string, label: string, ref: string, kind: "file" | "artifact" = "file", landing: ProducedFile["landing"] = kind === "file" ? "landed" : null) =>
    ({ kind, label, ref, threadId, landing });
  const report = [
    thread(ids.question, "Check the revenue figures", 17, {
      group: "waiting", reason: "question", statusLine: "Which quarter's exchange rate should I use?", progress: { done: 2, total: 5 },
      files: [file(ids.question, "revenue.xlsx", "threads/revenue/revenue.xlsx", "file", "not_merged")],
    }),
    thread(ids.approval, "Send the draft to finance", 25, {
      group: "waiting", reason: "approval", statusLine: "Send an email to finance@example.com?",
    }),
    thread(ids.failed, "Convert the old reports", 60, {
      group: "waiting", reason: "failed", statusLine: "The PDF could not be opened: it is encrypted",
    }),
    thread(ids.working, "Draft the summary", 28, {
      group: "working", statusLine: "Writing the outlook", progress: { done: 3, total: 6 },
      files: [file(ids.working, "summary.docx", "threads/summary/summary.docx", "file", "redoing"), file(ids.working, "Sales chart", "art-1", "artifact")],
    }),
    thread(ids.computer, "Tidy the shared folder", 40, {
      group: "working", reason: "computer", statusLine: "Waiting for thinkpad",
      place: { kind: "device", deviceId: "d", deviceName: "thinkpad", online: false },
    }),
    thread(ids.idle, "Collect the sales data", 540, {
      group: "idle", statusLine: "Done: 4 regions",
      files: ["north", "south", "east"].map((region) => file(ids.idle, `${region}.csv`, `threads/sales/${region}.csv`, "file", region === "east" ? "undone" : "landed")),
    }),
    thread(ids.resolved, "Book the review meeting", 9_000, {
      group: "resolved", statusLine: "Booked for Monday", resolvedAt: ago(8_900),
    }),
  ];
  const project = (id: string, name: string, created: number, updated: number, threads: ThreadRow[]): Project => ({
    id, name, icon: null, createdAt: ago(created), updatedAt: ago(updated),
    waiting: threads.filter((found) => found.group === "waiting").length,
    working: threads.filter((found) => found.group === "working").length,
    goal: null, instructions: "", masterSessionId: id, coordinatorTier: null, threadTier: null,
  });
  return {
    projects: [
      { ...project(ids.report, "Quarterly report", 4_320, 17, report), goal: "The third quarter's report for the board" },
      project(ids.budget, "Budget", 14_400, 2_880, []),
    ],
    threads: { [ids.report]: report, [ids.budget]: [] },
    library: {
      [ids.report]: [
        { path: "brief.docx", origin: "added", threadId: null, size: 18_342, updatedAt: ago(600), place: cloud },
        { path: "threads/summary/summary.docx", origin: "produced", threadId: ids.working, size: 52_118, updatedAt: ago(28), place: cloud },
        { path: "threads/revenue/revenue.xlsx", origin: "produced", threadId: ids.question, size: 9_870, updatedAt: ago(17), place: cloud },
      ],
      [ids.budget]: [],
    },
    // The revenue sheet: a routine made it (pruned since), a thread changed it, you saved it, and the
    // thread's next version did not land.
    history: {
      [ids.report]: {
        "threads/revenue/revenue.xlsx": [
          { id: "12:f", path: "threads/revenue/revenue.xlsx", by: { kind: "thread", threadId: ids.question, title: "Check the revenue figures" },
            at: ago(17), change: "changed", merged: false, available: true, landingId: null },
          { id: "12:p", path: "threads/revenue/revenue.xlsx", by: { kind: "you" }, at: ago(17), change: "changed", merged: true, available: true, landingId: null },
          { id: "9:f", path: "threads/revenue/revenue.xlsx", by: { kind: "thread", threadId: ids.question, title: "Check the revenue figures" },
            at: ago(120), change: "changed", merged: true, available: true, landingId: "9" },
          { id: "3:p", path: "threads/revenue/revenue.xlsx", by: { kind: "routine", name: "Nightly import" }, at: ago(140_000), change: "added", merged: true, available: false, landingId: null },
        ],
        // A forecast a thread made and you deleted: gone from the files, with its History still.
        "old-forecast.xlsx": [
          { id: "14:p", path: "old-forecast.xlsx", by: { kind: "you" }, at: ago(45), change: "deleted", merged: true, available: true, landingId: null },
          { id: "5:f", path: "old-forecast.xlsx", by: { kind: "thread", threadId: ids.idle, title: "Collect the sales data" },
            at: ago(4_000), change: "added", merged: true, available: true, landingId: "5" },
        ],
      },
      [ids.budget]: {},
    },
    deleted: {
      [ids.report]: {
        files: [{ id: "14:p", path: "old-forecast.xlsx", by: { kind: "you" }, at: ago(45), change: "deleted", merged: true, available: true, landingId: null }],
        more: false,
      },
      [ids.budget]: { files: [], more: false },
    },
    routines: {
      [ids.report]: [],
      [ids.budget]: [
        { id: "r-1", name: "Monthly spend check", scheduleDisplay: "On the 1st of every month at 09:00", nextRunAt: ago(-6_000), status: "active" },
        { id: "r-2", name: "Weekly cash report", scheduleDisplay: "Every Monday at 08:00", nextRunAt: ago(-2_000), status: "active" },
      ],
    },
  };
}
