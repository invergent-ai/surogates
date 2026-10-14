// The VM helper's protocol (spec, Section 11): the program of the app's own that runs the VM where Node cannot,
// macOS's surogate-vm (darwin.ts), spoken to in JSON lines on its stdin and stdout. Its lines heard, its own
// sent, its last words kept, as each backend that runs one speaks it.

import type { ChildProcess, SpawnOptions } from "node:child_process";
import { createInterface } from "node:readline";

import { ended } from "./sockets.js";

/** How the helper is started: through spawnClean, so it holds nothing of this process's but its stdio. */
export type Start = (program: string, args: string[], options: SpawnOptions & { env: NodeJS.ProcessEnv }) => ChildProcess;

/** What the helper says, one JSON object a line. */
export type Said =
  | { type: "listening" | "started" }
  | { type: "failed"; why: string }
  | { type: "shared"; id: number; tag: string; name?: string }
  | { type: "unshared"; id: number }
  | { type: "refused"; id?: number; why: string }
  | { type: "stopped"; reason: string; why?: string };

/** Settles with "late" at *deadline* (performance.now()), its timer not holding the process. */
export const late = (deadline: number) => new Promise<"late">((resolve) => {
  setTimeout(() => resolve("late"), Math.max(0, deadline - performance.now())).unref();
});

const exited = (child: ChildProcess) => new Promise<void>((resolve) => {
  if (ended(child)) resolve();
  else child.once("exit", () => resolve());
  child.once("error", () => resolve());
});

/** The helper, its lines heard, its stderr's end kept. */
export class Helper {
  readonly child: ChildProcess;
  readonly gone: Promise<void>;
  private said = "";
  // The last words of its stopped or failed line.
  last = "";
  private readonly listeners = new Set<(said: Said) => void>();

  /** *program* with *args*, started by *start* with the environment *env* alone. */
  constructor(start: Start, program: string, args: string[], env: NodeJS.ProcessEnv) {
    this.child = start(program, args, { stdio: ["pipe", "pipe", "pipe"], env });
    this.child.on("error", () => {});
    this.child.stdin?.on("error", () => {});
    this.child.stderr?.on("data", (chunk: Buffer) => {
      this.said = (this.said + chunk.toString()).slice(-4000);
    });
    if (this.child.stdout) {
      createInterface({ input: this.child.stdout, crlfDelay: Infinity }).on("line", (line) => {
        let said: Said;
        try {
          said = JSON.parse(line) as Said;
        } catch {
          return;
        }
        if (said.type === "failed") this.last = said.why;
        if (said.type === "stopped") this.last = said.why ?? "";
        for (const listener of this.listeners) listener(said);
      });
    }
    this.gone = exited(this.child);
  }

  send(line: object): void {
    if (!ended(this.child)) this.child.stdin?.write(`${JSON.stringify(line)}\n`);
  }

  on(listener: (said: Said) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The first line *wanted* takes, by *deadline*, or what ended the wait: the helper's exit, or the deadline. */
  async first(wanted: (said: Said) => boolean, deadline: number): Promise<Said | "exited" | "late"> {
    let stop = () => {};
    const heard = new Promise<Said>((resolve) => {
      stop = this.on((said) => {
        if (wanted(said)) resolve(said);
      });
    });
    try {
      return await Promise.race([heard, this.gone.then(() => "exited" as const), late(deadline)]);
    } finally {
      stop();
    }
  }

  /** Its last words: its stopped or failed line's, else the end of what it said on stderr. */
  words(): string {
    return this.last || this.said.trim();
  }
}
