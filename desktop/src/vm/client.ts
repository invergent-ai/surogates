// The main process's side of the VM manager (spec, Section 11): one manager for the
// app, shared by every device, started at the first process operation and again
// after one that went. In the app it is an Electron utility process; in the tests
// and the cross-check, a Node child process. Its guest goes with it (pdeathsig).

import { fork } from "node:child_process";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { setTimeout as wait } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { HostUser } from "../guest/protocol.js";
import type { NetworkAnswer, NetworkAsk } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { Backoff } from "./backoff.js";
import { type ProcessesChange, unavailable, type VmOperation, type VmOptions } from "./manager.js";

// The same from src/vm and from dist/vm.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
export const MANAGER = join(PACKAGE, "dist", "vm", "main.js");
// Past the guest's own 5 s to power off once asked, and the manager's exit after it.
const STOP_MS = 10_000;
// A ping to the manager every PING_MS; MISSED in a row unanswered is a manager that hangs.
const PING_MS = 10_000;
const MISSED = 3;
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
  // The app's answer to an ask of the host proxy's.
  | { type: "answer"; id: number; allow: boolean }
  // The keepalive, answered by a pong.
  | { type: "ping" }
  // The computer woke from sleep (Electron's powerMonitor).
  | { type: "resume" }
  | { type: "stop" };

export type FromManager =
  // It runs, and took its start.
  | { type: "ready" }
  | { type: "pong" }
  | { type: "result"; id: string; outcome: Outcome }
  // Unasked: a root's background processes in the guest changed.
  | { type: "processes"; root: string; change: ProcessesChange }
  // A connection of a root's command waits for its user's word on a destination off the package hosts.
  | ({ type: "ask"; id: number; root: string } & NetworkAsk)
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

// Who answers a root's ask: the device whose chat it is, or null for a root that is not its.
export type Asker = (root: string, asked: NetworkAsk) => Promise<NetworkAnswer> | null;

export interface VmClientOptions {
  vm: VmOptions;
  spawn?: () => ManagerProcess;
  teardownMs?: number;
  pingMs?: number;
}

export class VmClient {
  private manager: ManagerProcess | null = null;
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private stopping: Promise<void> | null = null;
  private teardowns = 0;
  private readonly listeners = new Set<(root: string, change: ProcessesChange) => void>();
  private readonly askers = new Set<Asker>();
  // The roots whose processes the manager has told of: a manager that goes takes them with its guest.
  private readonly told = new Set<string>();
  // A manager that went by itself is started again once this has passed (Section 11, Lifecycle).
  private readonly backoff = new Backoff();
  // Pings in a row the manager has not answered.
  private missed = 0;

  constructor(private readonly options: VmClientOptions) {}

  /**
   * One process operation of a root's, in the guest. A cancel is answered at once; the
   * manager is told. One that comes while a manager that went backs off waits for it.
   * Never rejects.
   */
  async perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (!this.manager && !this.stopping && this.backoff.wait > 0) {
      try {
        await wait(this.backoff.wait, undefined, { signal });
      } catch {
        return CANCELLED;
      }
    }
    if (this.stopping) return unavailable("is stopping");
    if (signal.aborted) return CANCELLED;
    let manager: ManagerProcess;
    try {
      manager = this.manager ?? this.start();
    } catch (error) {
      this.backoff.down();
      return unavailable(`did not start: ${error instanceof Error ? error.message : String(error)}`);
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

  /** The computer woke: its manager is told, and the pings it missed meanwhile are not held against it. */
  resume(): void {
    this.missed = 0;
    if (!this.stopping) this.manager?.send({ type: "resume" });
  }

  /** Asked about each root's destination off the package hosts, whichever device's it is. Returns what stops it. */
  onAsk(asker: Asker): () => void {
    this.askers.add(asker);
    return () => void this.askers.delete(asker);
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
    // One that answers no ping for MISSED of them hangs: it is killed, and its guest goes with it.
    this.missed = 0;
    const keepalive = setInterval(() => {
      if (this.missed >= MISSED) return manager.kill();
      this.missed += 1;
      manager.send({ type: "ping" });
    }, this.options.pingMs ?? PING_MS);
    keepalive.unref();
    manager.onMessage((message) => {
      if (message.type === "ready") {
        ran = true;
        this.backoff.up();
      } else if (message.type === "pong") this.missed = 0;
      else if (message.type === "result") this.pending.get(message.id)?.(message.outcome);
      else if (message.type === "processes") this.tell(message.root, message.change);
      else if (message.type === "ask") this.asked(manager, message);
      else if (message.type === "stopped") manager.kill();
    });
    manager.onExit(() => {
      clearInterval(keepalive);
      if (this.manager === manager) this.manager = null;
      // One that went by itself, or hung: the next is started once its backoff has passed.
      if (!this.stopping) this.backoff.down();
      // Before what it ran is answered: the next operation finds them ended.
      for (const root of this.told) this.tell(root, { gone: true });
      const outcome = ran ? SANDBOX_STOPPED : unavailable("did not start: its manager exited");
      for (const answer of [...this.pending.values()]) answer(outcome);
    });
    manager.send({ type: "start", options: this.options.vm });
    return manager;
  }

  // The first asker whose root it is answers; with none, or one that fails, it is denied. One
  // that throws is passed by: only the root's own device claims it, so another cannot let it through.
  private asked(manager: ManagerProcess, { id, root, host, port, privateNetwork }: Extract<FromManager, { type: "ask" }>): void {
    let answer: Promise<NetworkAnswer> | null = null;
    for (const asker of this.askers) {
      try {
        answer = asker(root, { host, port, privateNetwork });
      } catch {
        continue;
      }
      if (answer) break;
    }
    void (answer ?? Promise.resolve<NetworkAnswer>("deny")).catch((): NetworkAnswer => "deny").then((choice) => {
      manager.send({ type: "answer", id, allow: choice === "allow" || choice === "allow_session" });
    });
  }

  private tell(root: string, change: ProcessesChange): void {
    if ("gone" in change) this.told.delete(root);
    else this.told.add(root);
    for (const listener of this.listeners) listener(root, change);
  }
}
