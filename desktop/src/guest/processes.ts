// A root's background processes: start, poll, read_output, wait, kill,
// write_stdin and list_processes, answered in the shapes of the cloud's process
// registry (surogates/tools/utils/process_registry.py). The records live in the
// guest agent, one registry a root (root.ts), not in its runner, so they outlive
// it; their handles go to the host, which keeps them.

import { randomBytes } from "node:crypto";
import { constants as osConstants } from "node:os";
import { TextDecoder } from "node:util";

import { Failure, osError, type Refusal, sandboxError, valueError } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import { CANCELLED, type CommandChild, type CommandEnd } from "./command.js";
import { capStrings, firstPoints, lastPoints, splitLines, stripAnsi } from "./output.js";
import type { SpawnRequest } from "./protocol.js";

export const MAX_OUTPUT_CHARS = 200_000;
export const FINISHED_TTL_SECONDS = 1800;
export const MAX_PROCESSES = 64;
// The cloud's TERMINAL_TIMEOUT: wait's default and its most.
export const MAX_WAIT_SECONDS = 180;
export const KILL_GRACE_MS = 2_000;
// How much of a process's command, task id and output its handle keeps, in code points:
// the host keeps every handle, and sends them back with each operation.
export const HANDLE_CHARS = 2_000;
export const APP_QUIT = "The process ended when the app quit";
export const RUNNER_GONE = "The process ended because the computer's sandbox stopped";
export const RESTARTED = "The process was stopped because the computer restarted its sandbox; start it again if you still need it";
export const OUT_OF_MEMORY = "The computer's sandbox ran out of memory and ended this process, or one it started";
export const restartNotice = (reason: string) =>
  `The computer restarted its sandbox because ${reason}, and stopped your background processes; start them again if you still need them.`;
export const TOO_MANY = `This computer is already running ${MAX_PROCESSES} background processes for this chat; stop one before starting another.`;

// What the folder's record keeps of a process, to answer for it after the app quit.
export interface ProcessHandle {
  id: string;
  command: string;
  cwd: string;
  task_id: string | null;
  started_at: number; // seconds since the epoch
  // Once it has ended: how, and the last of what it said, as wait shows it.
  ended?: { exit_code: number | null; output: string; note: string | null };
}

// A process in the session runner, as the registry sees it.
export interface Spawned extends CommandChild {
  readonly started: Promise<number | null>;
  signal(signal: NodeJS.Signals): void;
  // Why the runner refused it, or null.
  write(data: Buffer): Promise<string | null>;
}

export interface Spawner {
  spawn(request: SpawnRequest): Spawned;
}

// Where a command would run, as its runner sees the folder: the folder a start's
// workdir resolves to, and why it cannot be entered (an errno name), or null.
export interface Placed {
  cwd: string;
  unenterable: string | null;
}

export interface ProcessesOptions {
  // run's workdir checks for a start, asked where its command will run: throws the refusal.
  place(workdir: string | null, signal: AbortSignal): Promise<Placed>;
  // The root's session runner, started at the first start; rejects when it cannot start,
  // or when *signal* cancels the start that waits for it.
  runner(signal: AbortSignal): Promise<Spawner>;
  // Why a command may not run now, or null (the hook guard).
  refusal?(): Promise<Outcome | null>;
  // Told how many processes are alive, after each change.
  live?(count: number): void;
  // Told every handle to keep, each time a process starts or ends.
  save?(handles: ProcessHandle[]): void;
  // Handles from before the app last quit.
  ended?: readonly ProcessHandle[];
  // Told each process's id once, when it has ended: what was kept for it can go.
  done?(id: string): void;
  // How much of its processes' output it keeps together, in UTF-16 code units; unbounded without.
  keep?: number;
  now?(): number; // seconds
}

interface Tracked {
  handle: ProcessHandle;
  pid: number | null;
  child: Spawned | null;
  // Cut down to MAX_OUTPUT_CHARS code points in bulk; read through output().
  buffer: string;
  // The buffer's length after its last cut: the next comes MAX_OUTPUT_CHARS later.
  kept: number;
  decoder: TextDecoder;
  // Without a pty, \r and \r\n read as \n, as the cloud's universal newlines do;
  // held: a \r that ended a chunk, until the next shows whether \n follows.
  newlines: boolean;
  held: boolean;
  exited: boolean;
  exitCode: number | null;
  note: string | null;
  // Stopped by kill: the cloud's -15, however it ended.
  killed: boolean;
  // Why its runner was restarted while it lived: it ends with RESTARTED, however it goes.
  restarted: string | null;
  failed: string | null;
  // Called once at its end.
  waiters: Set<() => void>;
}

// The handles of a root whose guest went: each process still running ended with it, as *note* says.
export const lostWith = (handles: readonly ProcessHandle[], note = RUNNER_GONE): ProcessHandle[] =>
  handles.map((handle) => handle.ended ? handle : { ...handle, ended: { exit_code: null, output: "", note } });

const notFound = (id: string) => ({ status: "not_found", error: `No process with ID ${id}` });
// The answers that carry a restart's notice as a note of their own.
const NOTED = new Set(["poll", "read_output", "wait", "kill", "write_stdin"]);

// Resolves at the record's end, after *ms* or at *signal*, whichever comes first,
// and leaves no timer, listener or waiter behind.
function settled(record: Tracked, ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (record.exited || signal?.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      record.waiters.delete(done);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms).unref();
    record.waiters.add(done);
    signal?.addEventListener("abort", done, { once: true });
  });
}
const output = (record: Tracked) => lastPoints(record.buffer, MAX_OUTPUT_CHARS);
const status = (record: Tracked) => (record.exited ? "exited" : "running");

// The record's buffer cut to its last *points*. A cut that shortens it is copied into a
// string of its own: V8's slice keeps the whole string it was cut from, which no length
// counts. The copy keeps lone surrogates as they are.
function cut(record: Tracked, points: number): void {
  const last = lastPoints(record.buffer, points);
  if (last.length < record.buffer.length) record.buffer = Buffer.from(last, "utf16le").toString("utf16le");
  record.kept = record.buffer.length;
}

// time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds)).
function localStamp(seconds: number): string {
  const at = new Date(seconds * 1000);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())}T${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`;
}

function string(args: Record<string, unknown>, name: string): string {
  const value = args[name];
  if (typeof value !== "string") throw valueError(`'${name}' must be a string`);
  return value;
}

function integer(args: Record<string, unknown>, name: string, fallback: number): number {
  const value = args[name] ?? fallback;
  if (typeof value !== "number" || !Number.isInteger(value)) throw valueError(`'${name}' must be an integer`);
  return value;
}

export class Processes {
  // In the cloud's order: running by start, finished by the time they ended.
  private readonly running = new Map<string, Tracked>();
  private readonly finished = new Map<string, Tracked>();
  private readonly now: () => number;
  private saving = false;
  // A restart's notice, until an answer carries it, and the processes that restart ended.
  private notice: { text: string; ids: Set<string> } | null = null;

  constructor(private readonly options: ProcessesOptions) {
    this.now = options.now ?? (() => Date.now() / 1000);
    for (const handle of options.ended ?? []) {
      if (this.now() - handle.started_at > FINISHED_TTL_SECONDS) continue;
      const record = this.record(handle, null);
      record.exited = true;
      record.exitCode = handle.ended?.exit_code ?? null;
      record.buffer = handle.ended?.output ?? "";
      record.note = handle.ended ? handle.ended.note : APP_QUIT;
      this.finished.set(handle.id, record);
    }
  }

  // How many are alive.
  get live(): number {
    return this.running.size;
  }

  // One operation's outcome. Never rejects.
  async answer(kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    try {
      const value = await this.dispatch(kind, args, signal);
      return value === CANCELLED ? CANCELLED : { ok: capStrings(this.told(kind, value)) };
    } catch (error) {
      const refusal = error instanceof Failure
        ? error.refusal
        : { type: "other", message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) };
      return { error: capStrings(refusal) as Refusal };
    }
  }

  private dispatch(kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<unknown> | unknown {
    if (kind === "start") return this.start(args, signal);
    if (kind === "list_processes") return this.list(args.task_id);
    const id = string(args, "session_id");
    const record = this.running.get(id) ?? this.finished.get(id);
    if (!record) return notFound(id);
    if (kind === "poll") return this.poll(record);
    if (kind === "read_output") return this.read(record, integer(args, "offset", 0), integer(args, "limit", 200));
    if (kind === "wait") return this.wait(record, args.timeout, signal);
    if (kind === "kill") return this.kill(record);
    if (kind === "write_stdin") return this.write(record, string(args, "data"));
    throw new Failure({ type: "unsupported", message: `This computer cannot do '${kind}' yet` });
  }

  // Before the host stops the runner to restart it: every live process will end
  // with RESTARTED, and once one has, the next answer that can carry it gets the notice.
  restart(reason: string): void {
    // One still starting is flagged too, because the runner may yet spawn it; it
    // raises the notice only once it has a pid.
    for (const record of this.running.values()) record.restarted = reason;
  }

  // The notice, once, for a run's output.
  takeNotice(): string | null {
    const text = this.notice?.text ?? null;
    this.notice = null;
    return text;
  }

  // The notice on the first answer that can carry it: a note of its own, or, in a
  // list, on each process the restart ended. A start has nowhere to put it.
  private told(kind: string, value: unknown): unknown {
    const notice = this.notice;
    if (!notice) return value;
    if (kind === "list_processes" && Array.isArray(value)) {
      const entries = value as Array<Record<string, unknown>>;
      const marked = entries.map((entry) => (notice.ids.has(entry.session_id as string) ? { ...entry, note: notice.text } : entry));
      if (marked.some((entry, i) => entry !== entries[i])) this.notice = null;
      return marked;
    }
    if (!NOTED.has(kind) || typeof value !== "object" || value === null || "note" in value) return value;
    this.notice = null;
    return { ...value, note: notice.text };
  }

  private async start(args: Record<string, unknown>, signal: AbortSignal): Promise<unknown> {
    const command = string(args, "command");
    const requested = args.workdir ?? null;
    if (requested !== null && typeof requested !== "string") throw valueError("'workdir' must be a string or null");
    const taskId = args.task_id ?? null;
    if (taskId !== null && typeof taskId !== "string") throw valueError("'task_id' must be a string or null");
    // notify_on_complete and watcher_interval are taken and ignored: nothing in the cloud reads them yet.
    // A cancel does not wait out the runner's answer, a restart, or the runner's start.
    const cancelled = new Promise<never>((_resolve, reject) => {
      if (signal.aborted) reject(CANCELLED);
      signal.addEventListener("abort", () => reject(CANCELLED), { once: true });
    });
    let placed: Placed;
    try {
      placed = await Promise.race([this.options.place(requested, signal), cancelled]);
    } catch (error) {
      if (signal.aborted) return CANCELLED;
      throw error;
    }
    // The cloud resolves the workdir before it sees the NUL, and Popen sees the NUL
    // before it enters the workdir.
    const { cwd } = placed;
    if (command.includes("\0")) throw valueError("embedded null byte");
    if (placed.unenterable) throw osError(placed.unenterable, cwd);
    await this.refuse();
    this.prune();
    if (this.running.size >= MAX_PROCESSES) throw sandboxError(TOO_MANY);
    let runner: Spawner;
    try {
      runner = await Promise.race([this.options.runner(signal), cancelled]);
    } catch (error) {
      if (signal.aborted) return CANCELLED;
      // The sandbox's own refusal, such as too many protected paths, is the agent's to read.
      if (error instanceof Failure) throw error;
      const why = error instanceof Error ? error.message : String(error);
      throw new Failure({ type: "unavailable", message: `This computer could not start the sandbox for background processes: ${why}` });
    }
    if (signal.aborted) return CANCELLED;
    // Again: starts that came together each passed the check above before any was counted.
    this.prune();
    if (this.running.size >= MAX_PROCESSES) throw sandboxError(TOO_MANY);
    const pty = args.pty === true;
    const handle: ProcessHandle = { id: `proc_${randomBytes(6).toString("hex")}`, command, cwd, task_id: taskId, started_at: this.now() };
    const child = runner.spawn({ id: handle.id, command, cwd, env: { PYTHONUNBUFFERED: "1" }, pty, stdin: true });
    const record = this.record(handle, child, pty);
    this.running.set(handle.id, record);
    this.changed();
    const pid = await child.started;
    if (pid === null) {
      this.running.delete(handle.id);
      this.finished.delete(handle.id);
      this.changed();
      throw new Failure({ type: "other", message: record.failed ?? RUNNER_GONE });
    }
    return { session_id: handle.id, pid };
  }

  private poll(record: Tracked): unknown {
    return {
      session_id: record.handle.id,
      command: record.handle.command,
      status: status(record),
      pid: record.pid,
      uptime_seconds: Math.trunc(this.now() - record.handle.started_at),
      output_preview: stripAnsi(lastPoints(record.buffer, 1000)),
      ...(record.exited ? { exit_code: record.exitCode } : {}),
      ...(record.note ? { note: record.note } : {}),
    };
  }

  private read(record: Tracked, offset: number, limit: number): unknown {
    const lines = splitLines(stripAnsi(output(record)));
    const selected = offset === 0 && limit > 0 ? lines.slice(-limit) : lines.slice(offset, offset + limit);
    return {
      session_id: record.handle.id,
      status: status(record),
      output: selected.join("\n"),
      total_lines: lines.length,
      showing: `${selected.length} lines`,
      ...(record.note ? { note: record.note } : {}),
    };
  }

  // Returns as the process exits; the cloud looks once a second.
  private async wait(record: Tracked, requested: unknown, signal: AbortSignal): Promise<unknown> {
    if (requested !== null && requested !== undefined && (typeof requested !== "number" || !Number.isFinite(requested))) {
      throw valueError("'timeout' must be a number or null");
    }
    const clamped = typeof requested === "number" && requested > MAX_WAIT_SECONDS
      ? `Requested wait of ${requested}s was clamped to configured limit of ${MAX_WAIT_SECONDS}s`
      : null;
    const effective = clamped ? MAX_WAIT_SECONDS : (requested as number | null | undefined) || MAX_WAIT_SECONDS;
    // As the cloud's loop: a wait of no time at all sees nothing, not even an exit.
    if (effective > 0) {
      await settled(record, effective * 1000, signal);
      if (signal.aborted) return CANCELLED;
      if (record.exited) {
        return {
          status: "exited",
          exit_code: record.exitCode,
          output: stripAnsi(lastPoints(record.buffer, 2000)),
          ...(clamped ? { timeout_note: clamped } : {}),
          ...(record.note ? { note: record.note } : {}),
        };
      }
    }
    return {
      status: "timeout",
      output: stripAnsi(lastPoints(record.buffer, 1000)),
      timeout_note: clamped ?? `Waited ${effective}s, process still running`,
    };
  }

  // SIGTERM to the process and all it started; SIGKILL after KILL_GRACE_MS.
  private async kill(record: Tracked): Promise<unknown> {
    if (record.exited) return { status: "already_exited", exit_code: record.exitCode };
    record.killed = true;
    record.child?.signal("SIGTERM");
    await settled(record, KILL_GRACE_MS);
    if (!record.exited) {
      record.child?.signal("SIGKILL");
      await settled(record, KILL_GRACE_MS);
    }
    // A runner that does not answer still leaves the record ended, as the cloud's does.
    this.end(record, { code: null, signal: "SIGTERM" });
    return { status: "killed", session_id: record.handle.id };
  }

  // Input to a shell runs commands: the hook guard's refusal holds here too.
  private async write(record: Tracked, data: string): Promise<unknown> {
    await this.refuse();
    if (record.exited) return { status: "already_exited", error: "Process has already finished" };
    const refused = await record.child?.write(Buffer.from(data, "utf8"));
    // The cloud's answer to a write that fails.
    if (refused) return { status: "error", error: refused };
    // Python's len(): code points.
    return { status: "ok", bytes_written: Array.from(data).length };
  }

  // What the hook guard refuses, thrown as its answer.
  private async refuse(): Promise<void> {
    const refused = await this.options.refusal?.();
    if (refused && "error" in refused) throw new Failure(refused.error);
  }

  private list(taskId: unknown): unknown {
    return [...this.running.values(), ...this.finished.values()]
      .filter((record) => !taskId || record.handle.task_id === taskId)
      .map((record) => ({
        session_id: record.handle.id,
        command: firstPoints(record.handle.command, 200),
        cwd: record.handle.cwd,
        pid: record.pid,
        started_at: localStamp(record.handle.started_at),
        uptime_seconds: Math.trunc(this.now() - record.handle.started_at),
        status: status(record),
        output_preview: lastPoints(record.buffer, 200),
        ...(record.exited ? { exit_code: record.exitCode } : {}),
        ...(record.note ? { note: record.note } : {}),
      }));
  }

  private record(handle: ProcessHandle, child: Spawned | null, pty = false): Tracked {
    const record: Tracked = {
      handle, pid: null, child, buffer: "", kept: 0, decoder: new TextDecoder("utf-8", { ignoreBOM: true }),
      newlines: !pty, held: false, exited: false, exitCode: null, note: null, killed: false, restarted: null, failed: null,
      waiters: new Set(),
    };
    if (child) {
      void child.started.then((pid) => {
        record.pid = pid;
        // Its start and its end in one chunk from the runner: the end came first.
        if (pid !== null && record.exited) this.noticed(record);
      });
      child.onOutput((chunk) => this.push(record, record.decoder.decode(chunk, { stream: true })));
      child.onEnd((end) => this.end(record, end));
    }
    return record;
  }

  // last: the end of its output, where a held \r is a newline.
  private push(record: Tracked, text: string, last = false): void {
    if (record.newlines) {
      if (record.held) text = `\r${text}`;
      record.held = !last && text.endsWith("\r");
      if (record.held) text = text.slice(0, -1);
      text = text.replace(/\r\n?/g, "\n");
    }
    record.buffer += text;
    // Measured from the last cut, so astral output, two units a code point, is cut as seldom.
    if (record.buffer.length > record.kept + MAX_OUTPUT_CHARS) cut(record, MAX_OUTPUT_CHARS);
    this.within();
  }

  // Its processes' output together within keep: past it, first what each holds beyond what
  // it shows (a buffer holds up to twice MAX_OUTPUT_CHARS between its own cuts), then
  // finished processes' output, the earliest ended first, then the running ones', the
  // longest first, each down to what its handle keeps (HANDLE_CHARS).
  private within(): void {
    const { keep } = this.options;
    if (keep === undefined) return;
    const records = [...this.running.values(), ...this.finished.values()];
    let total = 0;
    for (const record of records) total += record.buffer.length;
    if (total <= keep) return;
    for (const record of records) {
      if (record.buffer.length <= MAX_OUTPUT_CHARS) continue;
      const before = record.buffer.length;
      cut(record, MAX_OUTPUT_CHARS);
      total -= before - record.buffer.length;
    }
    const running = [...this.running.values()].sort((a, b) => b.buffer.length - a.buffer.length);
    for (const record of [...this.finished.values(), ...running]) {
      if (total <= keep) return;
      const before = record.buffer.length;
      cut(record, HANDLE_CHARS);
      total -= before - record.buffer.length;
    }
  }

  // Once: its exit code (128 + N for a signal, as a shell says; -15 after kill),
  // or null and a note when its sandbox went first.
  private end(record: Tracked, end: CommandEnd): void {
    if (record.exited) return;
    if ("failed" in end) record.failed = end.failed;
    this.push(record, record.decoder.decode(), true);
    record.exited = true;
    record.child = null;
    if (record.restarted !== null) record.note = RESTARTED;
    else if (record.killed) record.exitCode = -15;
    else if ("lost" in end) record.note = RUNNER_GONE;
    else if ("code" in end) {
      record.exitCode = end.code ?? 128 + (end.signal ? osConstants.signals[end.signal] : 0);
      if (end.oom) record.note = OUT_OF_MEMORY;
    }
    if (record.pid !== null) this.noticed(record);
    this.running.delete(record.handle.id);
    this.finished.set(record.handle.id, record);
    for (const waiter of record.waiters) waiter();
    this.options.done?.(record.handle.id);
    this.changed();
  }

  // A restart's notice is due once one of its processes has ended. One that never
  // started has its start's answer instead.
  private noticed(record: Tracked): void {
    if (record.restarted === null) return;
    this.notice = { text: restartNotice(record.restarted), ids: new Set([...(this.notice?.ids ?? []), record.handle.id]) };
  }

  // One save for every change in the same tick: a runner that dies ends all its
  // processes at once. A start awaits its child after this, so its save lands first.
  private changed(): void {
    this.options.live?.(this.running.size);
    if (this.saving || !this.options.save) return;
    this.saving = true;
    queueMicrotask(() => {
      this.saving = false;
      this.options.save?.(this.handles());
    });
  }

  // How each process ended, kept for the next registry, as the cloud keeps it for 30
  // minutes; one still running is kept as it started, and the next says it ended when the app quit.
  handles(): ProcessHandle[] {
    return [...this.running.values(), ...this.finished.values()].map((record) => {
      const { command, task_id: taskId } = record.handle;
      const handle = { ...record.handle, command: firstPoints(command, HANDLE_CHARS), task_id: taskId === null ? null : firstPoints(taskId, HANDLE_CHARS) };
      if (!record.exited) return handle;
      return { ...handle, ended: { exit_code: record.exitCode, output: lastPoints(record.buffer, HANDLE_CHARS), note: record.note } };
    });
  }

  // _prune_if_needed: finished records older than the TTL, from their start; then,
  // at the limit, the oldest finished one.
  private prune(): void {
    const now = this.now();
    for (const [id, record] of this.finished) {
      if (now - record.handle.started_at > FINISHED_TTL_SECONDS) this.finished.delete(id);
    }
    if (this.running.size + this.finished.size >= MAX_PROCESSES && this.finished.size > 0) {
      const oldest = [...this.finished.values()].reduce((a, b) => (b.handle.started_at < a.handle.started_at ? b : a));
      this.finished.delete(oldest.handle.id);
    }
  }
}
