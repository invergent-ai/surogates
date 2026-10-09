// The root runner: every command of a root runs in here, in the root's own
// namespaces in the guest, as the root's own user, so a server one command
// starts is reachable from the next (spec, Section 11). The agent speaks
// ToRunner on stdin and reads FromRunner on stdout, after a first
// {"ready":true}; it ends the runner by ending its stdin, and the namespaces go
// with it. In the guest, enter-root gives it the root's cgroup, delegated to it
// (--cgroups <folder>): each command gets a cgroup of its own there, which ends it
// with everything it started; and the root's socket to the host proxy (--tunnel
// <socket>), for the proxies it listens on before any command runs (listeners.ts),
// and for each connection into the root it is asked to bring (dial).
// The host's tests start it bare, without either: a command's process group is then all it signals.

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, readFileSync, rmdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import type { Readable } from "node:stream";
import { parseArgs } from "node:util";

import { Failure } from "../files/answers.js";
import { findOnPath } from "../files/operations.js";
import { unenterable, workdir } from "./command.js";
import { carryIn, listen } from "./listeners.js";
import { KILL_GRACE_MS } from "./processes.js";
import type { FromRunner, SpawnRequest, ToRunner } from "./protocol.js";

// The root's cgroup in the guest, where its memory is counted. A run's cgroup is in its run
// folder, a background process's in its proc folder. And the root's socket to the host proxy.
const { cgroups: CGROUPS = null, tunnel: TUNNEL = null } = parseArgs({ options: { cgroups: { type: "string" }, tunnel: { type: "string" } } }).values;
// The cgroups of commands that have ended, each removed once nothing of it runs.
const ended = new Set<string>();
// What waits for a process that does not read its input stays in the runner's memory: this much, at most.
const MAX_STDIN_BYTES = 1024 * 1024;

interface Child {
  proc: ChildProcess;
  // Its cgroup in the guest; null without --cgroups. A run's is the runner's own to make and
  // remove; a background process's, the agent's (root.ts, ProcessCgroups).
  cgroup: string | null;
  // A command with no stdin is a foreground run: what it leaves running ends with it.
  foreground: boolean;
  killed: boolean;
  exited: boolean;
  done: boolean;
  // The root's out-of-memory kills when it started.
  ooms: number;
}

// One word on a bash line: in '...', an embedded quote written as '\''. A copy
// of hosts/policy.ts's, which the agent does not ship.
const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

const children = new Map<string, Child>();
const paused = new Set<Readable>();

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
// without --cgroups, its process group.
function signalAll(child: Child, signal: NodeJS.Signals): void {
  if (!child.cgroup) {
    try {
      if (child.proc.pid !== undefined) process.kill(-child.proc.pid, signal);
    } catch {
      // The group has gone.
    }
    return;
  }
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

// How many processes the kernel has ended for memory in the root's cgroup, its only one
// that counts memory (root.ts, contain); 0 without one.
function oomKills(): number {
  try {
    return CGROUPS ? Number(/^oom_kill (\d+)$/m.exec(readFileSync(join(CGROUPS, "memory.events"), "utf8"))?.[1] ?? 0) : 0;
  } catch {
    return 0;
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

function finish(id: string): void {
  const child = children.get(id);
  if (!child || child.done) return;
  child.done = true;
  children.delete(id);
  // Killed: what it left may hold its output open, and nothing more is read.
  child.proc.stdout?.destroy();
  child.proc.stderr?.destroy();
  // A background process the kernel ended for memory: by SIGKILL, itself or the shell or
  // terminal it ran in, while the root's kills rose. One that only lost a child goes unnoted.
  const killed = child.proc.signalCode === "SIGKILL" || child.proc.exitCode === 137;
  const oom = !child.foreground && killed && oomKills() > child.ooms;
  say({ type: "exit", id, code: child.proc.exitCode, signal: child.proc.signalCode as NodeJS.Signals | null, ...(oom ? { oom } : {}) });
  if (child.foreground) retire(child.cgroup);
}

function start(request: SpawnRequest): void {
  const { id } = request;
  if (children.has(id)) {
    say({ type: "error", id, message: "a process with this id is already running" });
    return;
  }
  const env = { ...process.env, ...request.env };
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
  const child: Child = { proc, cgroup, foreground: !request.stdin, killed: false, exited: false, done: false, ooms: oomKills() };
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
    if (child.foreground) signalAll(child, "SIGKILL");
    if (child.killed) finish(id);
  });
  proc.on("close", () => finish(id));
}

function signal(id: string, name: NodeJS.Signals): void {
  const child = children.get(id);
  if (!child) return;
  if (name === "SIGKILL") child.killed = true;
  signalAll(child, name);
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
  } else if (message.type === "dial") {
    // A connection the browser makes to a server of this root's: dialed in here, so in the root's own
    // network namespace, and brought to the agent on the root's socket. A runner started bare has none.
    if (TUNNEL && typeof message.id === "string") void carryIn(TUNNEL, message.id, message.port, message.first);
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
// The agent is done with this root: every command ends, and all it started.
process.stdin.on("end", () => {
  for (const child of children.values()) signalAll(child, "SIGKILL");
  process.exit(0);
});
// Its proxies listen before any command runs: one that cannot ends the runner, and the root is not set up.
if (TUNNEL) await listen(TUNNEL);
process.stdout.write('{"ready":true}\n');
