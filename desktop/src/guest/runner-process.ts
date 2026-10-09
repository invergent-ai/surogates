// The agent's side of a root runner (runner.ts): one command in it as a
// RunnerChild, and the runner's questions answered by id.

import type { ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";

import type { CommandChild, CommandEnd } from "./command.js";
import type { Answer, FromRunner, Question, SpawnRequest, ToRunner } from "./protocol.js";

const READY_TIMEOUT_MS = 15_000;
const STOP_MS = 2_000;
// The runner's own lines are at most about 90 KB.
const MAX_LINE_BYTES = 4 * 1024 * 1024;

// One command in the runner, as the host sees it.
export class RunnerChild implements CommandChild {
  pid: number | null = null;
  // Its pid once it runs; null when it could not start or its runner went first.
  readonly started: Promise<number | null>;
  private settleStarted: (pid: number | null) => void = () => {};
  private output: (chunk: Buffer, err: boolean) => void = () => {};
  private end: ((end: CommandEnd) => void) | null = null;
  private ended: CommandEnd | null = null;
  // Writes the runner has not answered yet, in the order they were sent.
  private readonly writes: Array<(refused: string | null) => void> = [];

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

  // Why the runner refused it, or null.
  write(data: Buffer): Promise<string | null> {
    if (this.ended) return Promise.resolve(null);
    return new Promise((resolve) => {
      this.writes.push(resolve);
      this.send({ type: "stdin", id: this.id, data: data.toString("base64") });
    });
  }

  // True once it has ended.
  receive(message: FromRunner): boolean {
    if (message.type === "started") {
      this.pid = message.pid;
      this.settleStarted(message.pid);
    } else if (message.type === "data" && typeof message.data === "string") {
      this.output(Buffer.from(message.data, "base64"), message.err === true);
    } else if (message.type === "exit") {
      this.finish({ code: message.code, signal: message.signal, ...(message.oom ? { oom: true as const } : {}) });
    } else if (message.type === "written") {
      this.writes.shift()?.(null);
    } else if (message.type === "error" && message.stdin) {
      this.writes.shift()?.(message.message);
    } else if (message.type === "error") {
      this.finish({ failed: message.message });
    }
    return this.ended !== null;
  }

  finish(end: CommandEnd): void {
    if (this.ended) return;
    this.ended = end;
    this.settleStarted(this.pid);
    for (const written of this.writes.splice(0)) written(null);
    this.end?.(end);
  }
}

export class SessionRunner {
  readonly ready: Promise<void>;
  // Settles once the runner's process has gone, however it went.
  readonly gone: Promise<void>;
  private readonly children = new Map<string, RunnerChild>();
  // Questions the runner has not answered yet; each gets null if it goes first.
  private readonly questions = new Map<string, (answer: Answer | null) => void>();
  private settleReady: (error?: Error) => void = () => {};
  private left = false;
  private stopping = false;
  private stderr = "";

  // *child* is the runner's process: enter-root's, or, in the tests, the bare script.
  // *onLost* is told when it goes without being stopped; *readyMs* is how long it has to say it is ready.
  constructor(private readonly child: ChildProcess, onLost: () => void = () => {}, readyMs = READY_TIMEOUT_MS) {
    // A write to a runner that has died is not an error of its own: its exit is the one way out.
    child.stdin?.on("error", () => {});
    child.stderr?.on("data", (chunk: Buffer) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-4000);
    });
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`the session runner did not start: ${this.stderr}`)), readyMs);
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
        const asked = this.questions.get(message.id);
        if (asked && (message.type === "placed" || message.type === "refused" || message.type === "found")) {
          this.questions.delete(message.id);
          asked(message);
          return;
        }
        const target = this.children.get(message.id);
        if (target?.receive(message)) this.children.delete(message.id);
      });
      // A runner whose line never ends is broken: it is killed, and goes as one that died.
      // Only a command that took the runner over writes such a line: where Yama's ptrace_scope lets it.
      let unended = 0;
      child.stdout.on("data", (chunk: Buffer) => {
        const newline = chunk.lastIndexOf(0x0a);
        unended = newline < 0 ? unended + chunk.length : chunk.length - newline - 1;
        if (unended <= MAX_LINE_BYTES) return;
        child.kill("SIGKILL");
        child.stdout?.destroy();
      });
    }
    this.gone = new Promise((resolve) => {
      const leave = () => {
        if (this.left) return;
        this.left = true;
        this.settleReady(new Error(`the session runner exited: ${this.stderr}`));
        for (const target of this.children.values()) target.finish({ lost: true });
        this.children.clear();
        for (const asked of this.questions.values()) asked(null);
        this.questions.clear();
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

  // Whether its process has gone.
  get went(): boolean {
    return this.left;
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

  // The runner's answer to *question*, or null when it has gone, or goes first.
  ask(question: Question): Promise<Answer | null> {
    if (this.left) return Promise.resolve(null);
    // The runner would answer the two as one: the second is refused, and the runner is still there.
    if (this.questions.has(question.id)) {
      return Promise.resolve({ type: "refused", id: question.id, refusal: { type: "other", message: "A question with this id is already waiting" } });
    }
    return new Promise((resolve) => {
      this.questions.set(question.id, resolve);
      this.send(question);
    });
  }

  // Asks the runner for a connection to *port* of its root's own loopback, which it brings to the agent
  // on the root's socket under *id* (network.ts, arrival). A runner that has gone brings nothing.
  dial(id: string, port: number): void {
    if (!this.left) this.send({ type: "dial", id, port });
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
