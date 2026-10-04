// The host's side of a root's session runner (runner.ts): the runner is wrapped
// once with srt, from the folder, so srt's denies hold inside it for its whole
// life, and every command in it shares one network namespace.

import type { ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";

import type { FromRunner, SpawnRequest, ToRunner } from "./messages.js";
import type { CommandChild, CommandEnd } from "./run.js";

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
    } else if (message.type === "data") {
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
