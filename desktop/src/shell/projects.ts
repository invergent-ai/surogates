// The signed-in agent's projects (spec, Section 12), as its page serves them: the page holds
// the user's token, so it registers a ProjectsSource, and the shell calls it through the main
// process. The page's answers are server content, so each is checked against Section 12's
// types and copied field by field before the shell uses it.

import type {
  LibraryEntry, ProducedFile, Project, ProjectsSource, ProjectSummary, Routine, ThreadPlace, ThreadRow,
} from "../../../web/src/lib/projects-contract.js";

// How long the page has to answer a call.
export const ANSWER_TIMEOUT_MS = 10_000;

const METHODS = ["list", "get", "create", "update", "archive", "threads", "resolve", "reopen", "library", "routines"] as const;
type Method = (typeof METHODS)[number];

// What the main process sends the page's preload. A call's deadline is when its time runs out
// (Date.now()'s clock): a page that holds the call until it serves drops it after that.
export type ToPage =
  | { type: "call"; id: number; method: Method; args: unknown[]; deadline: number }
  | { type: "subscribe"; id: number; projectId: string }
  | { type: "unsubscribe"; id: number };

class Unusable extends Error {}

/** A call the page did not answer in time: one that changes something may have changed it all the same. */
export class TimedOut extends Error {}

const fields = (value: unknown): Record<string, unknown> => (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
const text = (value: unknown, max: number): value is string => typeof value === "string" && value.length <= max;
const named = (value: unknown): value is string => text(value, 500) && value !== "";
// ISO 8601 in UTC, with its Z (Section 12): a time with no zone would be read as this computer's local
// time, and a string that merely ends in Z is read however the engine guesses.
const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$/;
const time = (value: unknown): value is string => text(value, 40) && ISO_UTC.test(value) && !Number.isNaN(Date.parse(value));
const count = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 0 && (value as number) <= 1_000_000;
const one = <T>(value: unknown, allowed: readonly T[]): value is T => allowed.includes(value as T);
const tier = (value: unknown): value is Project["coordinatorTier"] => value === null || value === "basic" || value === "pro";
const need = (ok: boolean): void => {
  if (!ok) throw new Unusable();
};

function listOf<T>(value: unknown, max: number, item: (value: unknown) => T): T[] {
  need(Array.isArray(value) && value.length <= max);
  return (value as unknown[]).map(item);
}

function summaryOf(value: unknown): ProjectSummary {
  const { id, name, icon, createdAt, updatedAt, waiting, working } = fields(value);
  need(named(id) && named(name) && (icon === null || text(icon, 100)) && time(createdAt) && time(updatedAt) && count(waiting) && count(working));
  return { id, name, icon, createdAt, updatedAt, waiting, working } as ProjectSummary;
}

function projectOf(value: unknown): Project {
  const { goal, instructions, masterSessionId, coordinatorTier, threadTier } = fields(value);
  need((goal === null || text(goal, 16_000)) && text(instructions, 16_000) && named(masterSessionId) && tier(coordinatorTier) && tier(threadTier));
  return { ...summaryOf(value), goal, instructions, masterSessionId, coordinatorTier, threadTier } as Project;
}

function placeOf(value: unknown): ThreadPlace {
  const { kind, deviceId, deviceName, online } = fields(value);
  if (kind === "cloud") return { kind };
  need(kind === "device" && named(deviceId) && text(deviceName, 200) && typeof online === "boolean");
  return { kind: "device", deviceId, deviceName, online } as ThreadPlace;
}

function fileOf(value: unknown): ProducedFile {
  const { kind, label, ref, threadId } = fields(value);
  need(one(kind, ["file", "artifact"]) && text(label, 500) && text(ref, 4096) && named(threadId));
  return { kind, label, ref, threadId } as ProducedFile;
}

function threadOf(value: unknown): ThreadRow {
  const { id, title, group, reason, statusLine, progress, files, place, createdAt, updatedAt, resolvedAt } = fields(value);
  const { done, total } = fields(progress);
  need(named(id) && text(title, 500) && one(group, ["waiting", "working", "idle", "resolved"])
    && one(reason, ["question", "approval", "failed", "computer", null]) && (statusLine === null || text(statusLine, 2_000))
    && (progress === null || (count(done) && count(total))) && time(createdAt) && time(updatedAt)
    && (resolvedAt === null || time(resolvedAt)));
  return {
    id, title, group, reason, statusLine, progress: progress === null ? null : { done, total }, files: listOf(files, 200, fileOf),
    place: placeOf(place), createdAt, updatedAt, resolvedAt,
  } as ThreadRow;
}

function entryOf(value: unknown): LibraryEntry {
  const { path, origin, threadId, size, updatedAt, place } = fields(value);
  need(text(path, 4096) && one(origin, ["added", "produced"]) && (threadId === null || named(threadId))
    && (size === null || (Number.isSafeInteger(size) && (size as number) >= 0)) && (updatedAt === null || time(updatedAt)));
  return { path, origin, threadId, size, updatedAt, place: placeOf(place) } as LibraryEntry;
}

function routineOf(value: unknown): Routine {
  const { id, name, scheduleDisplay, nextRunAt, status } = fields(value);
  need(named(id) && text(name, 500) && text(scheduleDisplay, 500) && (nextRunAt === null || time(nextRunAt)) && text(status, 100));
  return { id, name, scheduleDisplay, nextRunAt, status } as Routine;
}

// The row a thread's call answers must be that thread's: the shell cannot tell a project's rows apart otherwise.
function theThread(row: ThreadRow, threadId: unknown): ThreadRow {
  need(row.id === threadId);
  return row;
}

// Each answer checked against what its call, with these *args*, returns.
const CHECKS: Record<Method, (value: unknown, args: unknown[]) => unknown> = {
  list: (value) => listOf(value, 500, summaryOf),
  get: projectOf,
  create: projectOf,
  update: projectOf,
  archive: (value) => need(value === undefined || value === null),
  // With a thread, the read is that row alone, or none once it is no longer one of the project's.
  threads: (value, [, threadId]) => {
    const rows = listOf(value, 500, threadOf);
    need(threadId === undefined || rows.length <= 1);
    return threadId === undefined ? rows : rows.map((row) => theThread(row, threadId));
  },
  resolve: (value, [, threadId]) => theThread(threadOf(value), threadId),
  reopen: (value, [, threadId]) => theThread(threadOf(value), threadId),
  library: (value) => listOf(value, 2_000, entryOf),
  routines: (value) => listOf(value, 200, routineOf),
};

interface Call {
  method: Method;
  args: unknown[];
  resolve(value: unknown): void;
  reject(error: Error): void;
  timer: NodeJS.Timeout;
}

export class PageProjects implements ProjectsSource {
  private next = 1;
  private readonly calls = new Map<number, Call>();
  private readonly subscriptions = new Map<number, (threadId: string | null) => void>();

  constructor(private readonly send: (message: ToPage) => void, private readonly timeoutMs = ANSWER_TIMEOUT_MS) {}

  list = () => this.call<ProjectSummary[]>("list");
  get = (projectId: string) => this.call<Project>("get", projectId);
  create = (input: { name: string; goal?: string; instructions?: string }) => this.call<Project>("create", input);
  update = (projectId: string, patch: Parameters<ProjectsSource["update"]>[1]) => this.call<Project>("update", projectId, patch);
  archive = (projectId: string) => this.call<void>("archive", projectId);
  threads = (projectId: string, threadId?: string) =>
    this.call<ThreadRow[]>("threads", ...(threadId === undefined ? [projectId] : [projectId, threadId]));
  resolve = (projectId: string, threadId: string) => this.call<ThreadRow>("resolve", projectId, threadId);
  reopen = (projectId: string, threadId: string) => this.call<ThreadRow>("reopen", projectId, threadId);
  library = (projectId: string) => this.call<LibraryEntry[]>("library", projectId);
  routines = (projectId: string) => this.call<Routine[]>("routines", projectId);

  subscribe(projectId: string, onChange: (threadId: string | null) => void): () => void {
    const id = this.next++;
    this.subscriptions.set(id, onChange);
    this.send({ type: "subscribe", id, projectId });
    return () => {
      if (this.subscriptions.delete(id)) this.send({ type: "unsubscribe", id });
    };
  }

  /** The page's answer to call *id*, `{ok}` or `{error}`: checked against what the call returns, then settled. */
  answered(id: unknown, outcome: unknown): void {
    const call = typeof id === "number" ? this.calls.get(id) : undefined;
    if (!call) return;
    this.calls.delete(id as number);
    clearTimeout(call.timer);
    const { ok, error } = fields(outcome);
    if (!("ok" in fields(outcome))) {
      call.reject(new Error(text(error, 500) && error ? error : `The agent's page could not answer ${call.method}`));
      return;
    }
    try {
      call.resolve(CHECKS[call.method](ok, call.args));
    } catch {
      call.reject(new Error(`The agent's page answered ${call.method} with something Surogate cannot use`));
    }
  }

  /** Subscription *id*'s project changed: in thread *threadId*, or project-wide when null. */
  changed(id: unknown, threadId: unknown): void {
    if (typeof id !== "number" || !(threadId === null || named(threadId))) return;
    this.subscriptions.get(id)?.(threadId);
  }

  /** The page withdrew its source, or went: what waits fails, and every subscription ends. */
  withdrawn(): void {
    for (const call of this.calls.values()) {
      clearTimeout(call.timer);
      call.reject(new Error("The agent's page no longer serves its projects"));
    }
    this.calls.clear();
    this.subscriptions.clear();
  }

  private call<T>(method: Method, ...args: unknown[]): Promise<T> {
    const id = this.next++;
    const { promise, resolve, reject } = Promise.withResolvers<T>();
    const timer = setTimeout(() => {
      this.calls.delete(id);
      reject(new TimedOut(`The agent's page did not answer ${method} in time`));
    }, this.timeoutMs);
    this.calls.set(id, { method, args, resolve: resolve as (value: unknown) => void, reject, timer });
    this.send({ type: "call", id, method, args, deadline: Date.now() + this.timeoutMs });
    return promise;
  }
}
