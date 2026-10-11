// The wire types of projects and their threads, as the spec freezes them (Section 12,
// "The wire types, and ProjectsSource"). A declaration file, so that desktop/, whose rootDir
// is src, can import it, as it imports the bridge contract. projects.ts re-exports it.
//
// What a client takes of a thread's row, a Library's file or a project that it does not know, as a later server may
// send: a value it does not know is unknown, a file of a kind it does not know is left out, and a field it does not
// know is ignored. It never refuses the row or the list for one, and never throws on one.

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
  // "unknown": a tier this client does not know, as a later server may send. Shown as unknown, and never sent back.
  coordinatorTier: Tier | "unknown";
  threadTier: Tier | "unknown";
}

export type ThreadPlace =
  | { kind: "cloud" }
  | { kind: "device"; deviceId: string; deviceName: string; online: boolean };

// A row lists only its files of a kind its client knows.
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
  group: ThreadGroup | null; // null: a group this client does not know
  // files: a file that did not merge, or a landing that escalated; null: none, or one this client does not know
  reason: "question" | "approval" | "failed" | "computer" | "files" | null;
  statusLine: string | null;
  progress: { done: number; total: number } | null;
  files: ProducedFile[]; // none where a row lists none
  // How many more files the thread changed or named than its files list: past the row's 200, or with a name this
  // client would refuse. Optional: a page built before rows counted them serves none, which is none more.
  moreFiles?: number;
  // The thread's newest landing with a file still as it landed: what its card's Undo undoes; null when it has
  // none. Optional, as a file's landing is: a page built before file history serves none.
  landingId?: string | null;
  // What its computer set aside of a thread's work there, out of its copy; null when it set none aside, and for a
  // thread in the cloud. Optional: a page built before set-asides were told serves none.
  setAside?: SetAside | null;
  place: ThreadPlace | null; // null: a place this client does not know, or none
  createdAt: string;
  updatedAt: string;
  resolvedAt: string | null;
}

// A copy or a repository of a thread's that its computer kept whole when it made it again, or a place of its folder's
// (its folder's earlier history, with the copies in it) that its computer's app set aside when the folder at its path
// was another, or its record could not be read; and when that was.
export interface SetAsideWhole {
  kind: "copy" | "repository" | "place";
  at: string; // UTC, ISO 8601 with Z
}

// What a thread's computer keeps set aside of its work, and what of that it no longer keeps: on that computer
// alone, in Surogate Desktop's data. Nothing brings one back yet.
export interface SetAside {
  kept: SetAsideWhole[]; // oldest first
  snapshots: number; // snapshots of what its copy held of its own when the copy was made other files
  gone: SetAsideWhole[]; // gone at the computer's bound, which keeps only the latest; oldest first
}

export interface LibraryEntry {
  path: string;
  origin: "added" | "produced" | null; // null: an origin this client does not know
  threadId: string | null; // set when produced
  size: number | null;
  updatedAt: string | null;
  place: ThreadPlace | null; // a local thread's files are on its computer; null: a place this client does not know, or none
}

// Who changed a file: the signed-in user, a thread by its title, or a routine by its name.
export type ChangedBy =
  | { kind: "you" }
  | { kind: "thread"; threadId: string; title: string }
  | { kind: "routine"; name: string };

// One version of a file in its History, newest first.
export interface FileVersion {
  id: string; // opaque, never shown
  path: string;
  by: ChangedBy | null; // null: someone this client has no name for, as a later agent may send
  at: string; // UTC, ISO 8601 with Z
  // How it came to be. A way this client does not know is taken as "changed".
  change: "added" | "changed" | "deleted" | "restored" | "undone";
  merged: boolean; // false: a thread's version that did not land, kept in history
  available: boolean; // false once pruned: listed, as no longer kept
  landingId: string | null; // the landing it came with
}

// What a Restore or an Undo did: the files it made a version again, or put back; each it left as it was, and
// who changed it since (null: no one this client has a name for), or that its version from before is no longer
// kept (pruned); and each whose edit it recorded first, as a version in its History, before it was written over.
export interface UndoResult {
  applied: string[];
  skipped: { path: string; by: ChangedBy | null; pruned: boolean }[];
  pickedUp: string[];
}

// The project's files that are gone, the newest first: each the version that deleted it.
export interface DeletedFiles {
  files: FileVersion[];
  more: boolean; // others may have been deleted before these, and are not listed
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
  // A tier is sent only as one the client knows: one it shows as unknown is left as it is.
  update(
    projectId: string,
    patch: Partial<Pick<Project, "name" | "icon" | "goal" | "instructions"> & { coordinatorTier: Tier; threadTier: Tier }>,
  ): Promise<Project>;
  archive(projectId: string): Promise<void>;
  // With threadId, that thread's row alone: none once it is no longer one of the project's threads.
  threads(projectId: string, threadId?: string): Promise<ThreadRow[]>;
  resolve(projectId: string, threadId: string): Promise<ThreadRow>;
  reopen(projectId: string, threadId: string): Promise<ThreadRow>;
  library(projectId: string): Promise<LibraryEntry[]>;
  routines(projectId: string): Promise<Routine[]>;
  // A file's History, newest first: the file is the cloud's, or a folder's on a computer.
  history(projectId: string, path: string, place: ThreadPlace): Promise<FileVersion[]>;
  // Open version: the file as that version left it, handed to the user to save.
  openVersion(projectId: string, input: { versionId: string; path: string }): Promise<void>;
  // Restore: the file made that version again, as a landing by you, your edit to it recorded first.
  restore(projectId: string, input: { versionId: string; path: string }): Promise<UndoResult>;
  // Undo: what a landing changed, or all of a thread's landings, put back as a landing by you, a file changed
  // since left as it is.
  undo(projectId: string, target: { landingId: string } | { threadId: string }): Promise<UndoResult>;
  // The Library lists the deleted files under its files, so that a deleted file's History is reached.
  deleted(projectId: string): Promise<DeletedFiles>;
  // threadId null: something project-wide changed; refetch the list
  subscribe(projectId: string, onChange: (threadId: string | null) => void): () => void;
}
