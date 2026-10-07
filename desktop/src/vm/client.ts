// The main process's side of the VM manager (spec, Section 11): one manager for the
// app, shared by every device, started at the first process operation and again
// after one that went. In the app it is an Electron utility process; in the tests
// and the cross-check, a Node child process. Its guest goes with it (pdeathsig).

import { fork } from "node:child_process";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { HostUser, ProtectedKey } from "../guest/protocol.js";
import type { Outcome } from "../link/protocol.js";
import { type ProcessesChange, unavailable, type VmOperation, type VmOptions } from "./manager.js";

// The same from src/vm and from dist/vm.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
export const MANAGER = join(PACKAGE, "dist", "vm", "main.js");
const STOP_MS = 5_000;
// Past the manager's own bounds on a teardown: 15 s for a setup under way, 15 s for the
// agent's answer, and 15 s for the share's removal.
const TEARDOWN_MS = 50_000;

/**
 * The VM's files for an app whose data is *dataDir*: the sessions disk and the
 * console log there, the sockets in a folder of this user's runtime folder that is
 * that data's own, so two apps never share one (a development build beside the
 * installed app, a test), and a boot's sweep reaches no other app's guest. Until the
 * image is delivered, the image is the one images/guest/build.sh built in this
 * repository, or SUROGATE_VM_IMAGE's folder, and the agent disk this package's
 * (npm run agent-disk).
 */
export function vmOptions(dataDir: string, user: HostUser, env: NodeJS.ProcessEnv = process.env): VmOptions {
  const image = env.SUROGATE_VM_IMAGE || join(PACKAGE, "..", "images", "guest", "out");
  return {
    kernel: join(image, "vmlinuz"),
    rootfs: join(image, "rootfs.img"),
    agentDisk: join(PACKAGE, "dist", "agent.img"),
    sessions: join(dataDir, "vm", "sessions.img"),
    // The user's own, 0700, made by logind; short enough for a vhost-user socket's 108 bytes.
    run: join(env.XDG_RUNTIME_DIR || `/run/user/${user.uid}`, "surogate", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`),
    console: join(dataDir, "logs", "vm-console.log"),
    user,
  };
}

export type ToManager =
  | { type: "start"; options: VmOptions }
  | { type: "op"; operation: VmOperation }
  | { type: "cancel"; id: string }
  // Everything of a root ends in the guest: its folder is being let go. Answered as a result.
  | { type: "teardown"; id: string; root: string }
  // A root's protected keys, found between its commands: read-only in its namespace.
  | { type: "protect"; root: string; keys: ProtectedKey[] }
  | { type: "stop" };

export type FromManager =
  // It runs, and took its start.
  | { type: "ready" }
  | { type: "result"; id: string; outcome: Outcome }
  // Unasked: a root's background processes in the guest changed.
  | { type: "processes"; root: string; change: ProcessesChange }
  // Its last word at a stop, after every answer, from a process that waits to be ended: a utility
  // process's postMessage has no callback, and an exit right after it can lose what it sent.
  | { type: "stopped" };

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
  // Its end: 'close' follows its exit, and a spawn that failed, which has no exit.
  let closed = false;
  child.once("close", () => {
    closed = true;
  });
  return {
    // A send to a manager that has gone: its exit is what counts.
    send: (message) => void child.send(message, (error) => error),
    onMessage: (listener) => void child.on("message", (message) => listener(message as FromManager)),
    onExit: (listener) => {
      if (closed) listener();
      else child.once("close", () => listener());
    },
    kill: () => void child.kill("SIGKILL"),
  };
}

export interface VmClientOptions {
  vm: VmOptions;
  spawn?: () => ManagerProcess;
  teardownMs?: number;
}

export class VmClient {
  private manager: ManagerProcess | null = null;
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private stopping: Promise<void> | null = null;
  private teardowns = 0;
  private readonly listeners = new Set<(root: string, change: ProcessesChange) => void>();
  // The roots whose processes the manager has told of: a manager that goes takes them with its guest.
  private readonly told = new Set<string>();

  constructor(private readonly options: VmClientOptions) {}

  /** One process operation of a root's, in the guest. A cancel is answered at once; the manager is told. Never rejects. */
  perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (this.stopping) return Promise.resolve(unavailable("is stopping"));
    if (signal.aborted) return Promise.resolve(CANCELLED);
    let manager: ManagerProcess;
    try {
      manager = this.manager ?? this.start();
    } catch (error) {
      return Promise.resolve(unavailable(`did not start: ${error instanceof Error ? error.message : String(error)}`));
    }
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

  /**
   * Everything of *root* ends in the guest, if a manager runs: its folder is being let
   * go. A manager that does not answer in time is wedged: it is killed, and its guest,
   * the root's processes in it, goes with it. Never rejects.
   */
  async teardown(root: string): Promise<void> {
    const manager = this.manager;
    if (!manager || this.stopping) return;
    const id = `teardown-${(this.teardowns += 1)}`;
    const answered = new Promise<void>((resolve) => {
      this.pending.set(id, () => {
        this.pending.delete(id);
        resolve();
      });
      manager.send({ type: "teardown", id, root });
    });
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<"late">((resolve) => {
      timer = setTimeout(() => resolve("late"), this.options.teardownMs ?? TEARDOWN_MS);
    });
    const settled = await Promise.race([answered, late]);
    clearTimeout(timer);
    if (settled !== "late") return;
    const gone = new Promise<void>((resolve) => manager.onExit(resolve));
    manager.kill();
    await gone;
  }

  /** A root's protected keys found between its commands: read-only in its namespace, if a manager runs. */
  protect(root: string, keys: ProtectedKey[]): void {
    if (!this.stopping) this.manager?.send({ type: "protect", root, keys });
  }

  /** Told each change of a root's processes in the guest, whichever device's root it is. Returns what stops it. */
  onProcesses(listener: (root: string, change: ProcessesChange) => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
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
    // Before its exit is listened for, so an exit told at once clears it.
    this.manager = manager;
    // A manager that exits before it says it runs never ran what it was given.
    let ran = false;
    manager.onMessage((message) => {
      if (message.type === "ready") ran = true;
      else if (message.type === "result") this.pending.get(message.id)?.(message.outcome);
      else if (message.type === "processes") this.tell(message.root, message.change);
      else if (message.type === "stopped") manager.kill();
    });
    manager.onExit(() => {
      if (this.manager === manager) this.manager = null;
      // Before what it ran is answered: the next operation finds them ended.
      for (const root of this.told) this.tell(root, { gone: true });
      const outcome = ran ? SANDBOX_STOPPED : unavailable("did not start: its manager exited");
      for (const answer of [...this.pending.values()]) answer(outcome);
    });
    manager.send({ type: "start", options: this.options.vm });
    return manager;
  }

  private tell(root: string, change: ProcessesChange): void {
    if ("gone" in change) this.told.delete(root);
    else this.told.add(root);
    for (const listener of this.listeners) listener(root, change);
  }
}
