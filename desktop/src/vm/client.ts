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
import { REACH_MS } from "./inbound.js";
import { type Boot, type ProcessesChange, unavailable, type VmOperation, type VmOptions, WAITS } from "./manager.js";

// The same from src/vm and from dist/vm.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
export const MANAGER = join(PACKAGE, "dist", "vm", "main.js");
// The image images/guest/build.sh builds in this repository: a development build's, with its manifest.
export const REPO_IMAGE = join(PACKAGE, "..", "images", "guest", "out");
// A ping to the manager every PING_MS; MISSED in a row unanswered is a manager that hangs.
const PING_MS = 10_000;
const MISSED = 3;
// Past the manager's own bounds, with KVM and emulated, as its last boot ran: its stop, past the
// guest's power-off and the manager's exit after it; and its teardown, past a setup under way, the
// agent's answer, and the share's removal.
const STOP_MS = (emulated: boolean) => WAITS[emulated ? "emulated" : "kvm"].powerOffMs + 5_000;
const TEARDOWN_MS = (emulated: boolean) => {
  const { setupMs, shareMs } = WAITS[emulated ? "emulated" : "kvm"];
  return 2 * setupMs + shareMs + 5_000;
};

/**
 * The VM's files for an app whose data is *dataDir*: the sessions disk and the
 * console log there, the sockets in a folder of this user's runtime folder that is
 * that data's own, so two apps never share one (a development build beside the
 * installed app, a test), and a boot's sweep reaches no other app's guest. The image
 * is SUROGATE_VM_IMAGE's folder when it is set, else the one the app delivers
 * (*delivered.image*), else the repository's; the agent disk is the app's
 * (*delivered.agentDisk*), else this package's (npm run agent-disk). SUROGATE_VM_KVM
 * names another device to open for KVM, as a test with none does.
 */
export function vmOptions(dataDir: string, user: HostUser, env: NodeJS.ProcessEnv = process.env, delivered: { image?: string; agentDisk?: string } = {}): VmOptions {
  const image = env.SUROGATE_VM_IMAGE || delivered.image || REPO_IMAGE;
  return {
    kernel: join(image, "vmlinuz"),
    rootfs: join(image, "rootfs.img"),
    agentDisk: delivered.agentDisk ?? join(PACKAGE, "dist", "agent.img"),
    ...(env.SUROGATE_VM_KVM ? { kvm: env.SUROGATE_VM_KVM } : {}),
    sessions: join(dataDir, "vm", "sessions.img"),
    // The user's own, 0700, made by logind; short enough for a vhost-user socket's 108 bytes.
    run: join(env.XDG_RUNTIME_DIR || `/run/user/${user.uid}`, "surogate", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`),
    console: join(dataDir, "logs", "vm-console.log"),
    user,
  };
}

/**
 * What of *env* the VM's files are made from: in a packaged app, nothing of SUROGATE_VM_IMAGE or
 * SUROGATE_VM_KVM, which are a development build's and the tests', so it boots only the image
 * its manifest checks, and opens /dev/kvm for KVM.
 */
export function vmEnv(env: NodeJS.ProcessEnv, packaged: boolean): NodeJS.ProcessEnv {
  if (!packaged) return env;
  const { SUROGATE_VM_IMAGE: _image, SUROGATE_VM_KVM: _kvm, ...rest } = env;
  return rest;
}

export type ToManager =
  | { type: "start"; options: VmOptions }
  | { type: "op"; operation: VmOperation }
  | { type: "cancel"; id: string }
  // Everything of a root ends in the guest: its folder is being let go. Answered as a result.
  | { type: "teardown"; id: string; root: string }
  // The app's answer to an ask of the host proxy's.
  | { type: "answer"; id: number; allow: boolean }
  // Whether something in a root listens on a port of its own loopback now. Answered as a result, its ok true or false.
  | { type: "listening"; id: string; root: string; port: number }
  // The keepalive, answered by a pong.
  | { type: "ping" }
  // The computer woke from sleep (Electron's powerMonitor).
  | { type: "resume" }
  // The user's Retry: the boot that did not start is forgotten, and the next operation boots at once.
  | { type: "retry" }
  | { type: "stop" };

export type FromManager =
  // It runs, and took its start.
  | { type: "ready" }
  | { type: "pong" }
  | { type: "result"; id: string; outcome: Outcome }
  // Unasked: a root's background processes in the guest changed.
  | { type: "processes"; root: string; change: ProcessesChange }
  // Unasked: a boot of the guest, and how it went.
  | { type: "boot"; boot: Boot }
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
  // Resolves once the VM can boot: what it needs of this computer and its image are here.
  // Rejects with why not, after "This computer's sandbox".
  ready?: (signal: AbortSignal) => Promise<void>;
  spawn?: () => ManagerProcess;
  teardownMs?: number;
  pingMs?: number;
}

export class VmClient {
  private manager: ManagerProcess | null = null;
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private stopping: Promise<void> | null = null;
  // Aborted at the stop: an operation waiting out the backoff is answered then, not when its wait ends.
  private readonly halted = new AbortController();
  private teardowns = 0;
  private readonly listeners = new Set<(root: string, change: ProcessesChange) => void>();
  private readonly askers = new Set<Asker>();
  // The roots whose processes the manager has told of: a manager that goes takes them with its guest.
  private readonly told = new Set<string>();
  // A manager that went by itself is started again once this has passed (Section 11, Lifecycle).
  private readonly backoff = new Backoff();
  // Pings in a row the manager has not answered.
  private missed = 0;
  private readonly boots = new Set<(boot: Boot) => void>();
  // Whether the last boot ran emulated: the manager's own bounds are then the emulated guest's.
  private emulated = false;
  private probes = 0;

  constructor(private readonly options: VmClientOptions) {}

  /**
   * One process operation of a root's, in the guest. A cancel is answered at once; the
   * manager is told. One that comes before the VM can boot, its image still downloading,
   * waits for it, and one that comes while a manager that went backs off waits for it,
   * until it is cancelled or the VM is stopped. Never rejects.
   */
  async perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (this.options.ready && !this.stopping) {
      try {
        await this.options.ready(AbortSignal.any([signal, this.halted.signal]));
      } catch (error) {
        if (this.stopping) return unavailable("is stopping");
        if (signal.aborted) return CANCELLED;
        return unavailable(error instanceof Error ? error.message : String(error));
      }
    }
    if (!this.manager && !this.stopping && this.backoff.wait > 0) {
      // Its cancel, or the stop, is answered just below.
      await wait(this.backoff.wait, undefined, { signal: AbortSignal.any([signal, this.halted.signal]) }).catch(() => {});
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
      timer = setTimeout(() => resolve("late"), this.options.teardownMs ?? TEARDOWN_MS(this.emulated));
    });
    const settled = await Promise.race([answered, late]);
    clearTimeout(timer);
    if (settled !== "late") return;
    const gone = new Promise<void>((resolve) => manager.onExit(resolve));
    manager.kill();
    await gone;
  }

  /**
   * Whether something in *root* listens on *port* of its own loopback now. False where no manager
   * runs, which none is started to ask, and when it does not say within the agent's own bound. Never rejects.
   */
  listening(root: string, port: number): Promise<boolean> {
    const manager = this.manager;
    if (!manager || this.stopping) return Promise.resolve(false);
    const id = `listening-${(this.probes += 1)}`;
    return new Promise((resolve) => {
      const timer = setTimeout(() => answer({ ok: false }), REACH_MS + 5_000);
      const answer = (outcome: Outcome) => {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve("ok" in outcome && outcome.ok === true);
      };
      this.pending.set(id, answer);
      manager.send({ type: "listening", id, root, port });
    });
  }

  /** The computer woke: its manager is told, and the pings it missed meanwhile are not held against it. */
  resume(): void {
    this.missed = 0;
    if (!this.stopping) this.manager?.send({ type: "resume" });
  }

  /** The user's Retry: the manager forgets the boot that did not start, so the next operation boots at once. */
  retry(): void {
    if (!this.stopping) this.manager?.send({ type: "retry" });
  }

  /** Asked about each root's destination off the package hosts, whichever device's it is. Returns what stops it. */
  onAsk(asker: Asker): () => void {
    this.askers.add(asker);
    return () => void this.askers.delete(asker);
  }

  /** Told each boot of the guest, and how it went. Returns what stops it. */
  onBoot(listener: (boot: Boot) => void): () => void {
    this.boots.add(listener);
    return () => void this.boots.delete(listener);
  }

  /** Told each change of a root's processes in the guest, whichever device's root it is. Returns what stops it. */
  onProcesses(listener: (root: string, change: ProcessesChange) => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  // The manager stops its guest and exits; one that does not is killed, and its guest goes with it.
  stop(): Promise<void> {
    this.halted.abort();
    this.stopping ??= (async () => {
      const manager = this.manager;
      if (!manager) return;
      const gone = new Promise<void>((resolve) => manager.onExit(resolve));
      const timer = setTimeout(() => manager.kill(), STOP_MS(this.emulated));
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
      else if (message.type === "boot") this.booted(message.boot);
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

  private booted(boot: Boot): void {
    if ("emulated" in boot) this.emulated = boot.emulated !== null;
    for (const listener of this.boots) listener(boot);
  }

  private tell(root: string, change: ProcessesChange): void {
    if ("gone" in change) this.told.delete(root);
    else this.told.add(root);
    for (const listener of this.listeners) listener(root, change);
  }
}
