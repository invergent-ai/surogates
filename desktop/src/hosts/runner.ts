// The session runner. Once a root starts a background process, every command of
// that root runs in here, inside one long-lived sandbox: each srt command
// otherwise gets its own network namespace, and could not reach a server another
// command started (spec, Section 1). The host speaks ToRunner on stdin and reads
// FromRunner on stdout, after a first {"ready":true}; it ends the runner by
// ending its stdin, and the sandbox goes with it.

import { type ChildProcess, spawn } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import type { Readable } from "node:stream";

import type { FromRunner, SpawnRequest, ToRunner } from "./messages.js";

// Every process a command starts inherits it, setsid or not: how the runner finds them all.
export const MARKER = "SUROGATE_PROCESS";
// A process can fork while a sweep goes by: a SIGKILL sweeps again until it finds none.
const SWEEPS = 10;

interface Child {
  proc: ChildProcess;
  // A command with no stdin is a foreground run: what it leaves running ends with it.
  foreground: boolean;
  killed: boolean;
  exited: boolean;
  done: boolean;
}

const children = new Map<string, Child>();
const paused = new Set<Readable>();
// srt's environment (the app's names, its proxy, TMPDIR), without the flag that makes Electron a Node.
const base: NodeJS.ProcessEnv = { ...process.env };
delete base.ELECTRON_RUN_AS_NODE;

// A host that reads slowly leaves a command's output in that command's pipe, not in here.
function say(message: FromRunner, from?: Readable | null): void {
  if (!process.stdout.write(`${JSON.stringify(message)}\n`) && from) {
    from.pause();
    paused.add(from);
  }
}
process.stdout.on("drain", () => {
  for (const stream of paused) stream.resume();
  paused.clear();
});

// A background process's stderr goes to its stdout, in the order it was written.
function argv(request: SpawnRequest): [string, string[]] {
  if (request.stdin) return ["bash", ["-c", 'exec 2>&1; exec bash -c "$1"', "bash", request.command]];
  return ["bash", ["-c", request.command]];
}

// The command's process group, then every process in the sandbox that carries its
// marker: one that called setsid or double-forked has left the group, not the
// marker. The sandbox has its own pid namespace, so /proc lists only its processes.
function sweep(id: string, pid: number | undefined, signal: NodeJS.Signals): void {
  try {
    if (pid !== undefined) process.kill(-pid, signal);
  } catch {
    // The group has gone.
  }
  const needle = `\0${MARKER}=${id}\0`;
  for (let pass = 0; pass < (signal === "SIGKILL" ? SWEEPS : 1); pass += 1) {
    let found = false;
    for (const name of readdirSync("/proc")) {
      if (!/^\d+$/.test(name) || Number(name) === process.pid) continue;
      try {
        if (!`\0${readFileSync(`/proc/${name}/environ`, "latin1")}`.includes(needle)) continue;
        process.kill(Number(name), signal);
        found = true;
      } catch {
        // Gone, or not ours to read.
      }
    }
    if (!found) return;
  }
}

function finish(id: string): void {
  const child = children.get(id);
  if (!child || child.done) return;
  child.done = true;
  children.delete(id);
  // Killed: what it left may hold its output open, and nothing more is read.
  child.proc.stdout?.destroy();
  child.proc.stderr?.destroy();
  say({ type: "exit", id, code: child.proc.exitCode, signal: child.proc.signalCode as NodeJS.Signals | null });
}

function start(request: SpawnRequest): void {
  const { id } = request;
  if (children.has(id)) {
    say({ type: "error", id, message: "a process with this id is already running" });
    return;
  }
  const env = { ...base, ...request.env, [MARKER]: id };
  const [file, args] = argv(request);
  let proc: ChildProcess;
  try {
    // Its own process group, so one signal reaches what it started too.
    proc = spawn(file, args, { cwd: request.cwd, env, detached: true, stdio: [request.stdin ? "pipe" : "ignore", "pipe", "pipe"] });
  } catch (error) {
    say({ type: "error", id, message: error instanceof Error ? error.message : String(error) });
    return;
  }
  const child: Child = { proc, foreground: !request.stdin, killed: false, exited: false, done: false };
  children.set(id, child);
  let started = false;
  // A command that stops reading its stdin is not an error of the runner's.
  proc.stdin?.on("error", () => {});
  proc.stdout?.on("data", (chunk: Buffer) => say({ type: "data", id, data: chunk.toString("base64") }, proc.stdout));
  proc.stderr?.on("data", (chunk: Buffer) => say({ type: "data", id, data: chunk.toString("base64"), err: true }, proc.stderr));
  proc.on("spawn", () => {
    started = true;
    say({ type: "started", id, pid: proc.pid ?? 0 });
  });
  proc.on("error", (error) => {
    if (started || child.done) return;
    child.done = true;
    children.delete(id);
    say({ type: "error", id, message: error.message });
  });
  proc.on("exit", () => {
    child.exited = true;
    // As in a sandbox of its own: a run's leftovers end when its shell does.
    if (child.foreground) sweep(id, proc.pid, "SIGKILL");
    if (child.killed) finish(id);
  });
  proc.on("close", () => finish(id));
}

function signal(id: string, name: NodeJS.Signals): void {
  const child = children.get(id);
  if (!child) return;
  if (name === "SIGKILL") child.killed = true;
  sweep(id, child.proc.pid, name);
  if (child.killed && child.exited) finish(id);
}

createInterface({ input: process.stdin }).on("line", (line) => {
  let message: ToRunner;
  try {
    message = JSON.parse(line) as ToRunner;
  } catch {
    return;
  }
  if (message.type === "spawn") {
    const { type: _type, ...request } = message;
    start(request);
  } else if (message.type === "signal") {
    signal(message.id, message.signal);
  } else if (message.type === "stdin") {
    children.get(message.id)?.proc.stdin?.write(Buffer.from(message.data, "base64"));
  }
});
// The host is done with this sandbox. Everything in the runner's pid namespace
// ends with it; outside a sandbox, as in the tests, so does each command.
process.stdin.on("end", () => {
  for (const [id, child] of children) sweep(id, child.proc.pid, "SIGKILL");
  process.exit(0);
});
process.stdout.write('{"ready":true}\n');
