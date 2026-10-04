// A root's background processes: start, poll, read_output, wait, kill,
// write_stdin and list_processes, answered in the shapes of the cloud's process
// registry (surogates/tools/utils/process_registry.py). The records live here in
// the host, not in the session runner, so they outlive it.

import { randomBytes } from "node:crypto";
import { constants as osConstants } from "node:os";
import { TextDecoder } from "node:util";

import { Failure, osError, type Refusal, sandboxError, valueError } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import type { SpawnRequest } from "./messages.js";
import { capStrings, firstPoints, lastPoints, splitLines, stripAnsi } from "./output.js";
import { CANCELLED, type CommandChild, type CommandContext, type CommandEnd, unenterable, workdir } from "./run.js";

export const MAX_OUTPUT_CHARS = 200_000;
export const FINISHED_TTL_SECONDS = 1800;
export const MAX_PROCESSES = 64;
// The cloud's TERMINAL_TIMEOUT: wait's default and its most.
export const MAX_WAIT_SECONDS = 180;
export const KILL_GRACE_MS = 2_000;
export const RUNNER_GONE = "The process ended because the computer's sandbox stopped";
export const TOO_MANY = `This computer is already running ${MAX_PROCESSES} background processes for this chat; stop one before starting another.`;

// A process as it was started.
export interface ProcessHandle {
  id: string;
  command: string;
  cwd: string;
  task_id: string | null;
  started_at: number; // seconds since the epoch
}

// A process in the session runner, as the registry sees it.
export interface Spawned extends CommandChild {
  readonly started: Promise<number | null>;
  signal(signal: NodeJS.Signals): void;
  write(data: Buffer): void;
}

export interface Spawner {
  spawn(request: SpawnRequest): Spawned;
}

export interface ProcessesOptions {
  context: CommandContext;
  // The root's session runner, started at the first start; rejects when it cannot start.
  runner(): Promise<Spawner>;
  // Why a command may not run now, or null (the hook guard).
  refusal?(): Promise<Outcome | null>;
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
  failed: string | null;
  // Called once at its end.
  waiters: Set<() => void>;
}

const notFound = (id: string) => ({ status: "not_found", error: `No process with ID ${id}` });

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

  constructor(private readonly options: ProcessesOptions) {
    this.now = options.now ?? (() => Date.now() / 1000);
  }

  // One operation's outcome. Never rejects.
  async answer(kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    try {
      const value = await this.dispatch(kind, args, signal);
      return value === CANCELLED ? CANCELLED : { ok: capStrings(value) };
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

  private async start(args: Record<string, unknown>, signal: AbortSignal): Promise<unknown> {
    const command = string(args, "command");
    const requested = args.workdir ?? null;
    if (requested !== null && typeof requested !== "string") throw valueError("'workdir' must be a string or null");
    const taskId = args.task_id ?? null;
    if (taskId !== null && typeof taskId !== "string") throw valueError("'task_id' must be a string or null");
    // notify_on_complete and watcher_interval are taken and ignored: nothing in the cloud reads them yet.
    // The cloud resolves the workdir before it sees the NUL, and Popen sees the NUL
    // before it enters the workdir.
    const cwd = workdir(this.options.context, requested);
    if (command.includes("\0")) throw valueError("embedded null byte");
    const code = unenterable(cwd);
    if (code) throw osError(code, cwd);
    const refused = await this.options.refusal?.();
    if (refused && "error" in refused) throw new Failure(refused.error);
    this.prune();
    if (this.running.size >= MAX_PROCESSES) throw sandboxError(TOO_MANY);
    let runner: Spawner;
    try {
      runner = await this.options.runner();
    } catch (error) {
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
    const pid = await child.started;
    if (pid === null) {
      this.running.delete(handle.id);
      this.finished.delete(handle.id);
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

  private write(record: Tracked, data: string): unknown {
    if (record.exited) return { status: "already_exited", error: "Process has already finished" };
    record.child?.write(Buffer.from(data, "utf8"));
    // Python's len(): code points.
    return { status: "ok", bytes_written: Array.from(data).length };
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
      newlines: !pty, held: false, exited: false, exitCode: null, note: null, killed: false, failed: null, waiters: new Set(),
    };
    if (child) {
      void child.started.then((pid) => {
        record.pid = pid;
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
    if (record.buffer.length > record.kept + MAX_OUTPUT_CHARS) {
      record.buffer = lastPoints(record.buffer, MAX_OUTPUT_CHARS);
      record.kept = record.buffer.length;
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
    if (record.killed) record.exitCode = -15;
    else if ("lost" in end) record.note = RUNNER_GONE;
    else if ("code" in end) record.exitCode = end.code ?? 128 + (end.signal ? osConstants.signals[end.signal] : 0);
    this.running.delete(record.handle.id);
    this.finished.set(record.handle.id, record);
    for (const waiter of record.waiters) waiter();
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
