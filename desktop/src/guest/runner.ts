// The root runner: every command of a root runs in here, in the root's own
// namespaces in the guest, as the root's own user, so a server one command
// starts is reachable from the next (spec, Section 11). The agent speaks
// ToRunner on stdin and reads FromRunner on stdout, after a first
// {"ready":true}; it ends the runner by ending its stdin, and the namespaces go
// with it. In the guest, enter-root gives it the root's cgroup, delegated to it
// (--cgroups <folder>): each command gets a cgroup of its own there, which ends it
// with everything it started. Until commands move into the VM, a tool host runs it
// the same way, wrapped once in srt, as the root's session runner, and finds a
// command's processes by its marker.

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import type { Readable } from "node:stream";

import { Failure } from "../files/answers.js";
import { findOnPath } from "../files/operations.js";
import { unenterable, workdir } from "./command.js";
import { KILL_GRACE_MS } from "./processes.js";
import type { FromRunner, SpawnRequest, ToRunner } from "./protocol.js";

// Every process a command starts inherits it, setsid or not: how the runner finds them all.
export const MARKER = "SUROGATE_PROCESS";
// The root's cgroup in the guest, or null under srt. A run's cgroup is in its run
// folder, a background process's in its proc folder, where its memory is counted.
const CGROUPS = process.argv[2] === "--cgroups" ? (process.argv[3] ?? null) : null;
// The cgroups of commands that have ended, each removed once nothing of it runs.
const ended = new Set<string>();
// A process can fork while a sweep goes by: a SIGKILL sweeps again until it finds none.
const SWEEPS = 10;
// What waits for a process that does not read its input stays in the runner's memory: this much, at most.
const MAX_STDIN_BYTES = 1024 * 1024;

interface Child {
  proc: ChildProcess;
  // Its cgroup in the guest; null under srt. A run's is the runner's own to make and
  // remove; a background process's, the agent's (root.ts, ProcessCgroups).
  cgroup: string | null;
  // A command with no stdin is a foreground run: what it leaves running ends with it.
  foreground: boolean;
  killed: boolean;
  exited: boolean;
  done: boolean;
}

// One word on a bash line: in '...', an embedded quote written as '\''. A copy
// of hosts/policy.ts's, which the agent does not ship.
const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

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

// The cloud's shapes: a terminal of 30 by 120 through script, which answers the
// command's own exit code (-e); without script, pipes. A background process's
// stderr goes to its stdout, in the order it was written.
function argv(request: SpawnRequest, env: NodeJS.ProcessEnv): [string, string[]] {
  // The script found is the one that runs: not a later lookup of the bare name.
  const script = request.pty ? findOnPath("script", env.PATH, request.cwd) : null;
  if (script) {
    return [script, ["-qfec", `stty rows 30 cols 120 2>/dev/null; exec bash -c ${quote(request.command)}`, "/dev/null"]];
  }
  if (request.stdin) return ["bash", ["-c", 'exec 2>&1; exec bash -c "$1"', "bash", request.command]];
  return ["bash", ["-c", request.command]];
}

// Every process of the command: in the guest, each in its cgroup, which a SIGKILL
// ends at once, whatever left the command's session or cleared its environment;
// under srt, its group and its marker's.
function signalAll(id: string, child: Child, signal: NodeJS.Signals): void {
  if (!child.cgroup) return sweep(id, child.proc.pid, signal);
  try {
    if (signal === "SIGKILL") return writeFileSync(join(child.cgroup, "cgroup.kill"), "1");
    for (const member of readFileSync(join(child.cgroup, "cgroup.procs"), "utf8").split("\n").filter(Boolean)) {
      try {
        process.kill(Number(member), signal);
      } catch {
        // Gone since.
      }
    }
  } catch {
    // Its cgroup has gone, and everything in it.
  }
}

// What the kernel noted in a background process's cgroup: that it ended one of its processes for memory.
function outOfMemory(cgroup: string | null): boolean {
  try {
    return cgroup !== null && /^oom_kill [1-9]/m.test(readFileSync(join(cgroup, "memory.events"), "utf8"));
  } catch {
    return false;
  }
}

// A command's cgroup once the command has ended, or could not start: removed once nothing of it runs.
function retire(cgroup: string | null): void {
  if (!cgroup) return;
  ended.add(cgroup);
  tidy();
}

// Each ended command's cgroup, once nothing of it runs: at its end, and at each start after.
function tidy(): void {
  for (const cgroup of ended) {
    try {
      rmdirSync(cgroup);
      ended.delete(cgroup);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") ended.delete(cgroup);
    }
  }
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
  const oom = outOfMemory(child.cgroup);
  say({ type: "exit", id, code: child.proc.exitCode, signal: child.proc.signalCode as NodeJS.Signals | null, ...(oom ? { oom } : {}) });
  if (child.foreground) retire(child.cgroup);
}

function start(request: SpawnRequest): void {
  const { id } = request;
  if (children.has(id)) {
    say({ type: "error", id, message: "a process with this id is already running" });
    return;
  }
  const env = CGROUPS ? { ...base, ...request.env } : { ...base, ...request.env, [MARKER]: id };
  let [file, args] = argv(request, env);
  const cgroup = CGROUPS && join(CGROUPS, request.stdin ? "proc" : "run", id);
  const own = request.stdin ? null : cgroup;
  let proc: ChildProcess;
  try {
    if (cgroup) {
      tidy();
      if (own) mkdirSync(own);
      // Its shell enters the command's cgroup before it runs the command, so all the command starts is there.
      [file, args] = ["/bin/sh", ["-c", 'echo $$ > "$0" && exec "$@"', join(cgroup, "cgroup.procs"), file, ...args]];
    }
    // Its own process group, so one signal reaches what it started too.
    proc = spawn(file, args, { cwd: request.cwd, env, detached: true, stdio: [request.stdin ? "pipe" : "ignore", "pipe", "pipe"] });
  } catch (error) {
    // A command past Linux's 128 KiB for one argument throws E2BIG here, its cgroup made.
    retire(own);
    say({ type: "error", id, message: error instanceof Error ? error.message : String(error) });
    return;
  }
  const child: Child = { proc, cgroup, foreground: !request.stdin, killed: false, exited: false, done: false };
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
    retire(own);
    say({ type: "error", id, message: error.message });
  });
  proc.on("exit", () => {
    child.exited = true;
    // As in a sandbox of its own: a run's leftovers end when its shell does.
    if (child.foreground) signalAll(id, child, "SIGKILL");
    if (child.killed) finish(id);
  });
  proc.on("close", () => finish(id));
}

function signal(id: string, name: NodeJS.Signals): void {
  const child = children.get(id);
  if (!child) return;
  if (name === "SIGKILL") child.killed = true;
  signalAll(id, child, name);
  // What outlives a SIGTERM ends once the registry's grace has passed, as the process itself would:
  // a leftover that ignores it, once the process has ended, has no SIGKILL to come but this.
  const { cgroup } = child;
  if (name === "SIGTERM" && cgroup) {
    setTimeout(() => {
      try {
        writeFileSync(join(cgroup, "cgroup.kill"), "1");
      } catch {
        // Its cgroup has gone, and everything in it.
      }
    }, KILL_GRACE_MS).unref();
  }
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
  } else if (message.type === "place") {
    // In this view and as this user: what the command will see.
    try {
      const cwd = workdir(message, message.workdir);
      say({ type: "placed", id: message.id, cwd, unenterable: unenterable(cwd) });
    } catch (error) {
      const refusal = error instanceof Failure ? error.refusal : { type: "other", message: String(error) };
      say({ type: "refused", id: message.id, refusal });
    }
  } else if (message.type === "which") {
    // As place is: a question the agent got wrong is answered, never thrown out of this handler with every command.
    try {
      say({ type: "found", id: message.id, found: findOnPath(message.name, process.env.PATH, message.cwd) !== null });
    } catch (error) {
      say({ type: "refused", id: message.id, refusal: { type: "other", message: String(error) } });
    }
  } else if (message.type === "stdin") {
    // Each is answered, in order: the host waits to know whether it was taken.
    const stdin = children.get(message.id)?.proc.stdin;
    if (stdin && stdin.writableLength > MAX_STDIN_BYTES) {
      say({ type: "error", id: message.id, message: "The process is not reading its input", stdin: true });
      return;
    }
    stdin?.write(Buffer.from(message.data, "base64"));
    say({ type: "written", id: message.id });
  }
});
// The host is done with this sandbox. Everything in the runner's pid namespace
// ends with it; outside a sandbox, as in the tests, so does each command.
process.stdin.on("end", () => {
  for (const [id, child] of children) signalAll(id, child, "SIGKILL");
  process.exit(0);
});
process.stdout.write('{"ready":true}\n');
