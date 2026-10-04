// The host's side of a root's session runner (runner.ts): the runner is wrapped
// once with srt, from the folder, so srt's denies hold inside it for its whole
// life, and every command in it shares one network namespace.

import { type ChildProcess, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import type { FromRunner, SpawnRequest, ToRunner } from "./messages.js";
import { hideSrtTmp, quote } from "./policy.js";
import { acquire, type CommandChild, type CommandContext, type CommandEnd, release } from "./run.js";

export const RUNNER = fileURLToPath(new URL("./runner.js", import.meta.url));

const READY_TIMEOUT_MS = 15_000;
const STOP_MS = 2_000;

// One command in the runner, as the host sees it.
export class RunnerChild implements CommandChild {
  pid: number | null = null;
  // Its pid once it runs; null when it could not start or its runner went first.
  readonly started: Promise<number | null>;
  private settleStarted: (pid: number | null) => void = () => {};
  private output: (chunk: Buffer, err: boolean) => void = () => {};
  private end: ((end: CommandEnd) => void) | null = null;
  private ended: CommandEnd | null = null;

  constructor(readonly id: string, private readonly send: (message: ToRunner) => void) {
    this.started = new Promise((resolve) => {
      this.settleStarted = resolve;
    });
  }

  onOutput(listener: (chunk: Buffer, err: boolean) => void): void {
    this.output = listener;
  }

  onEnd(listener: (end: CommandEnd) => void): void {
    this.end = listener;
    if (this.ended) listener(this.ended);
  }

  signal(signal: NodeJS.Signals): void {
    if (!this.ended) this.send({ type: "signal", id: this.id, signal });
  }

  kill(): void {
    this.signal("SIGKILL");
  }

  write(data: Buffer): void {
    if (!this.ended) this.send({ type: "stdin", id: this.id, data: data.toString("base64") });
  }

  // True once it has ended.
  receive(message: FromRunner): boolean {
    if (message.type === "started") {
      this.pid = message.pid;
      this.settleStarted(message.pid);
    } else if (message.type === "data" && typeof message.data === "string") {
      this.output(Buffer.from(message.data, "base64"), message.err === true);
    } else if (message.type === "exit") {
      this.finish({ code: message.code, signal: message.signal });
    } else if (message.type === "error") {
      this.finish({ failed: message.message });
    }
    return this.ended !== null;
  }

  finish(end: CommandEnd): void {
    if (this.ended) return;
    this.ended = end;
    this.settleStarted(this.pid);
    this.end?.(end);
  }
}

export class SessionRunner {
  readonly ready: Promise<void>;
  // Settles once the runner's process has gone, however it went.
  readonly gone: Promise<void>;
  private readonly children = new Map<string, RunnerChild>();
  private settleReady: (error?: Error) => void = () => {};
  private left = false;
  private stopping = false;
  private stderr = "";

  // *child* is the runner's process: srt's wrap of it, or, in the tests, the bare script.
  // *onLost* is told when it goes without being stopped.
  constructor(private readonly child: ChildProcess, onLost: () => void = () => {}) {
    // A write to a runner that has died is not an error of its own: its exit is the one way out.
    child.stdin?.on("error", () => {});
    child.stderr?.on("data", (chunk: Buffer) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-4000);
    });
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`the session runner did not start: ${this.stderr}`)), READY_TIMEOUT_MS);
      this.settleReady = (error) => {
        clearTimeout(timer);
        if (error) reject(error);
        else resolve();
      };
    });
    // Nobody may be waiting when a runner that was up goes.
    this.ready.catch(() => {});
    let up = false;
    if (child.stdout) {
      createInterface({ input: child.stdout }).on("line", (line) => {
        if (!up) {
          up = true;
          this.settleReady(line === '{"ready":true}' ? undefined : new Error(`the session runner said ${line}: ${this.stderr}`));
          return;
        }
        let message: FromRunner;
        try {
          message = JSON.parse(line) as FromRunner;
        } catch {
          return;
        }
        // The runner's sandbox runs the agent's commands: a line that is not a message is ignored.
        if (typeof message?.id !== "string") return;
        const target = this.children.get(message.id);
        if (target?.receive(message)) this.children.delete(message.id);
      });
    }
    this.gone = new Promise((resolve) => {
      const leave = () => {
        if (this.left) return;
        this.left = true;
        this.settleReady(new Error(`the session runner exited: ${this.stderr}`));
        for (const target of this.children.values()) target.finish({ lost: true });
        this.children.clear();
        if (!this.stopping) onLost();
        resolve();
      };
      child.once("exit", leave);
      // A runner that never spawned has no exit to come.
      child.once("error", () => {
        if (child.pid === undefined) leave();
      });
    });
  }

  spawn(request: SpawnRequest): RunnerChild {
    const target = new RunnerChild(request.id, (message) => this.send(message));
    if (this.left) {
      target.finish({ lost: true });
      return target;
    }
    // The runner refuses it too, and its answer would end the first.
    if (this.children.has(request.id)) {
      target.finish({ failed: "a process with this id is already running" });
      return target;
    }
    this.children.set(request.id, target);
    this.send({ type: "spawn", ...request });
    return target;
  }

  // Its stdin ends, and the runner goes with everything in its sandbox; one that does not is killed.
  async stop(): Promise<void> {
    this.stopping = true;
    this.child.stdin?.end();
    const timer = setTimeout(() => this.child.kill("SIGKILL"), STOP_MS);
    await this.gone;
    clearTimeout(timer);
  }

  private send(message: ToRunner): void {
    try {
      this.child.stdin?.write(`${JSON.stringify(message)}\n`);
    } catch {
      // The runner has gone; its exit ends what it ran.
    }
  }
}

// How the host ends its runner before its last look, which must find the runner
// gone: one that is up is stopped, and its SIGKILL bounds that. One still starting
// gets *ms*: ToolHosts kills a host that takes too long, and the next clears up after it.
export async function stopRunner(
  starting: Promise<Pick<SessionRunner, "stop">> | null, up: Pick<SessionRunner, "stop"> | null, ms: number,
): Promise<void> {
  if (up) return up.stop();
  await Promise.race([
    starting?.then((started) => started.stop(), () => {}),
    new Promise((resolve) => setTimeout(resolve, ms)),
  ]);
}

// A root's runner, wrapped with srt from the host's working folder, which is the
// session folder, and spawned as the host spawns a command: the app-built
// environment, srt's outer bash without startup files, /tmp/claude hidden.
// srt counts it as one command for its whole life, and its placeholders stay in
// the folder while it lives; it is released once, after its bwrap has gone.
export async function startRunner(context: CommandContext, onLost: () => void): Promise<SessionRunner> {
  acquire(context);
  let argv: string[];
  try {
    ({ argv } = await SandboxManager.wrapWithSandboxArgv(`${quote(process.execPath)} ${quote(RUNNER)}`));
  } catch (error) {
    // A wrap that rejects has released srt's count itself.
    release(context, false);
    throw error;
  }
  let child: ChildProcess;
  try {
    const [file, flag, line] = argv;
    if (!file || flag === undefined || line === undefined) throw new Error("srt returned no command");
    child = spawn(file, ["--norc", "--noprofile", flag, hideSrtTmp(line)], {
      cwd: context.folder,
      env: { ...context.env, ELECTRON_RUN_AS_NODE: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (error) {
    release(context);
    throw error;
  }
  const runner = new SessionRunner(child, onLost);
  void runner.gone.then(() => release(context));
  try {
    await runner.ready;
  } catch (error) {
    await runner.stop();
    throw error;
  }
  return runner;
}
