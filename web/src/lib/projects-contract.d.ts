// The wire types of projects and their threads, as the spec freezes them (Section 12,
// "The wire types, and ProjectsSource"). A declaration file, so that desktop/, whose rootDir
// is src, can import it, as it imports the bridge contract. projects.ts re-exports it.

export type ThreadGroup = "waiting" | "working" | "idle" | "resolved";
export type Tier = "basic" | "pro" | null; // null: the agent's own tier

export interface ProjectSummary {
  id: string;
  name: string;
  icon: string | null;
  createdAt: string; // UTC, ISO 8601 with Z
  updatedAt: string;
  waiting: number; // threads in Waiting on you, plus one for a question or approval open in the master
  working: number;
}

export interface Project extends ProjectSummary {
  goal: string | null;
  instructions: string;
  masterSessionId: string;
  coordinatorTier: Tier;
  threadTier: Tier;
}

export type ThreadPlace =
  | { kind: "cloud" }
  | { kind: "device"; deviceId: string; deviceName: string; online: boolean };

export interface ProducedFile {
  kind: "file" | "artifact";
  label: string;
  ref: string; // workspace-relative path, or artifact id
  threadId: string;
  // A file's state in the project's files, from the thread's landings; null for an artifact,
  // and for a file of a thread that works on the real files (a project over the file cap).
  // Optional: a page built before file history serves none. The shell takes that, and a mark it does
  // not know, as null.
  landing?: "landed" | "redoing" | "not_merged" | "undone" | null;
}

export interface ThreadRow {
  id: string; // the thread's session id
  title: string;
  group: ThreadGroup;
  reason: "question" | "approval" | "failed" | "computer" | "files" | null; // files: a file that did not merge, or a landing that escalated
  statusLine: string | null;
  progress: { done: number; total: number } | null;
  files: ProducedFile[];
  place: ThreadPlace;
  createdAt: string;
  updatedAt: string;
  resolvedAt: string | null;
}

export interface LibraryEntry {
  path: string;
  origin: "added" | "produced";
  threadId: string | null; // set when produced
  size: number | null;
  updatedAt: string | null;
  place: ThreadPlace; // a local thread's files are on its computer
}

export interface Routine {
  id: string;
  name: string;
  scheduleDisplay: string;
  nextRunAt: string | null;
  status: string;
}

export interface ProjectsSource {
  list(): Promise<ProjectSummary[]>;
  get(projectId: string): Promise<Project>;
  create(input: { name: string; goal?: string; instructions?: string }): Promise<Project>;
  update(
    projectId: string,
    patch: Partial<Pick<Project, "name" | "icon" | "goal" | "instructions" | "coordinatorTier" | "threadTier">>,
  ): Promise<Project>;
  archive(projectId: string): Promise<void>;
  // With threadId, that thread's row alone: none once it is no longer one of the project's threads.
  threads(projectId: string, threadId?: string): Promise<ThreadRow[]>;
  resolve(projectId: string, threadId: string): Promise<ThreadRow>;
  reopen(projectId: string, threadId: string): Promise<ThreadRow>;
  library(projectId: string): Promise<LibraryEntry[]>;
  routines(projectId: string): Promise<Routine[]>;
  // threadId null: something project-wide changed; refetch the list
  subscribe(projectId: string, onChange: (threadId: string | null) => void): () => void;
}
