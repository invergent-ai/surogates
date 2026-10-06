// The main process's side of the VM manager (spec, Section 11): one manager for the
// app, shared by every device, started at the first process operation and again
// after one that went. In the app it is an Electron utility process; in the tests
// and the cross-check, a Node child process. Its guest goes with it (pdeathsig).

import { fork } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { Outcome } from "../link/protocol.js";
import { unavailable, type VmOperation, type VmOptions } from "./manager.js";

// The same from src/vm and from dist/vm.
export const MANAGER = join(fileURLToPath(new URL("../..", import.meta.url)), "dist", "vm", "main.js");
const STOP_MS = 5_000;

export type ToManager =
  | { type: "start"; options: VmOptions }
  | { type: "op"; operation: VmOperation }
  | { type: "cancel"; id: string }
  // Everything of a root ends in the guest: its folder is being let go. Answered as a result.
  | { type: "teardown"; id: string; root: string }
  | { type: "stop" };

export type FromManager =
  // It runs, and took its start.
  | { type: "ready" }
  | { type: "result"; id: string; outcome: Outcome };

export interface ManagerProcess {
  send(message: ToManager): void;
  onMessage(listener: (message: FromManager) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
}

export function forkManager(script = MANAGER): ManagerProcess {
  // Its stdout goes to stderr: the app's stdout may carry other things.
  const child = fork(script, [], { stdio: ["ignore", 2, 2, "ipc"] });
  child.on("error", () => {});
  return {
    // A send to a manager that has gone: its exit is what counts.
    send: (message) => void child.send(message, (error) => error),
    onMessage: (listener) => void child.on("message", (message) => listener(message as FromManager)),
    onExit: (listener) => {
      if (child.exitCode !== null || child.signalCode !== null) listener();
      else child.once("exit", () => listener());
    },
    kill: () => void child.kill("SIGKILL"),
  };
}

export interface VmClientOptions {
  vm: VmOptions;
  spawn?: () => ManagerProcess;
}

export class VmClient {
  private manager: ManagerProcess | null = null;
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private stopping: Promise<void> | null = null;
  private teardowns = 0;

  constructor(private readonly options: VmClientOptions) {}

  /** One process operation of a root's, in the guest. A cancel is answered at once; the manager is told. Never rejects. */
  perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (this.stopping) return Promise.resolve(unavailable("is stopping"));
    if (signal.aborted) return Promise.resolve(CANCELLED);
    const manager = (this.manager ??= this.start());
    return new Promise((resolve) => {
      const answer = (outcome: Outcome) => {
        signal.removeEventListener("abort", cancel);
        this.pending.delete(operation.id);
        resolve(outcome);
      };
      const cancel = () => {
        manager.send({ type: "cancel", id: operation.id });
        answer(CANCELLED);
      };
      this.pending.set(operation.id, answer);
      signal.addEventListener("abort", cancel, { once: true });
      manager.send({ type: "op", operation });
    });
  }

  /** Everything of *root* ends in the guest, if a manager runs: its folder is being let go. Never rejects. */
  async teardown(root: string): Promise<void> {
    const manager = this.manager;
    if (!manager || this.stopping) return;
    const id = `teardown-${(this.teardowns += 1)}`;
    await new Promise<void>((resolve) => {
      this.pending.set(id, () => {
        this.pending.delete(id);
        resolve();
      });
      manager.send({ type: "teardown", id, root });
    });
  }

  // The manager stops its guest and exits; one that does not is killed, and its guest goes with it.
  stop(): Promise<void> {
    this.stopping ??= (async () => {
      const manager = this.manager;
      if (!manager) return;
      const gone = new Promise<void>((resolve) => manager.onExit(resolve));
      const timer = setTimeout(() => manager.kill(), STOP_MS);
      manager.send({ type: "stop" });
      await gone;
      clearTimeout(timer);
    })();
    return this.stopping;
  }

  private start(): ManagerProcess {
    const manager = (this.options.spawn ?? forkManager)();
    // A manager that exits before it says it runs never ran what it was given.
    let ran = false;
    manager.onMessage((message) => {
      if (message.type === "ready") ran = true;
      else if (message.type === "result") this.pending.get(message.id)?.(message.outcome);
    });
    manager.onExit(() => {
      if (this.manager === manager) this.manager = null;
      const outcome = ran ? SANDBOX_STOPPED : unavailable("did not start: its manager exited");
      for (const answer of [...this.pending.values()]) answer(outcome);
    });
    manager.send({ type: "start", options: this.options.vm });
    return manager;
  }
}
