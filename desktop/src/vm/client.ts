// The main process's side of the VM manager (spec, Section 11): one manager for the
// app, shared by every device, started at the first process operation and again
// after one that went. In the app it is an Electron utility process; in the tests
// and the cross-check, a Node child process. Its guest goes with it (pdeathsig).

import { fork } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
import { setTimeout as wait } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { HostUser } from "../guest/protocol.js";
import type { NetworkAnswer, NetworkAsk } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { Backoff, MOST_MS } from "./backoff.js";
import { type HistoryRequest, named, NOT_A_REQUEST, REFUSED } from "./history.js";
import { DOOR, REACH_MS } from "./inbound.js";
import { aborted, type Boot, late, LET_GO, type Place, type ProcessesChange, unavailable, type VmOperation, type VmOptions, WAITS } from "./manager.js";

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
// The manager's own bounds on a folder's place, which its answers are held to here.
interface PlaceWaits {
  setupMs: number;
  shareMs: number;
  historyMs: number;
  // A boot of the guest at its longest, however the guest will run, which no boot says before it has: its backoff's
  // wait, an emulated guest's hello, twice where the sessions disk could not be checked and the guest boots again on
  // a new one, and the power-off of a guest that was stopping when the boot was asked for.
  bootMs: number;
}
// Past those bounds, each from when the manager is asked (manager.ts, Guest.place, Guest.history, Guest.unplace):
// - a place's adding: a boot; its two folders' look and their shares, by one deadline, the agent's answer, and the
//   two shares' removal where the agent refused; then, for a place held already, the look that it is still there;
// - a request to its history: its place's adding, then the agent's answer, past which the manager gives the guest up;
// - a place's letting go: the manager's wait for what was asked of the place before it, a boot under way, the place
//   still being added, the agent's answer and the two shares' removal, one after the other.
const PLACE_MS = ({ bootMs, setupMs, shareMs }: PlaceWaits) => bootMs + setupMs + 4 * shareMs + 5_000;
const HISTORY_MS = (waits: PlaceWaits) => PLACE_MS(waits) + waits.historyMs;
const UNPLACE_MS = ({ bootMs, setupMs, shareMs }: PlaceWaits) => bootMs + 3 * setupMs + 5 * shareMs + 5_000;

const NOT_A_PLACE: Outcome = { error: { type: "value", message: "This names no place of a folder's history" } };
// Whether *place* has a place's fields, each of which the manager's guest reads as it is: no other is sent to it.
function whole(place: Place): boolean {
  if (typeof place !== "object" || place === null || typeof place.key !== "string" || typeof place.history !== "string") return false;
  return typeof place.real === "object" && place.real !== null && typeof place.real.path === "string";
}

// What a manager sent as its answer, where it is one: an ok, or an error of a type and words. Null for anything
// else, as a manager that ended while it wrote may leave.
function outcomeOf(sent: unknown): Outcome | null {
  if (typeof sent !== "object" || sent === null) return null;
  if (!("error" in sent)) return "ok" in sent ? (sent as Outcome) : null;
  const { error } = sent as { error: unknown };
  if (typeof error !== "object" || error === null) return null;
  const { type, message } = error as { type?: unknown; message?: unknown };
  return typeof type === "string" && typeof message === "string" ? (sent as Outcome) : null;
}

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
  // A folder's place for the agent's own git (spec, Section 13), added to the guest, booted for it; one request to
  // its history, the place added first; and the place let go. Each is answered as a result: a place's ok is null,
  // and a letting-go's true or false. A place and a request are stopped by a cancel of their id.
  | { type: "place"; id: string; place: Place }
  | { type: "history"; id: string; request: HistoryRequest }
  | { type: "unplace"; id: string; place: Place }
  // The app's answer to an ask of the host proxy's.
  | { type: "answer"; id: number; allow: boolean }
  // What the browser of the device that knocks with *key* may open: each port of a chat's own servers, and the chat's root.
  | { type: "forwards"; key: string; ports: Array<[number, string]> }
  // Whether something in a root listens on a port of its own loopback now. Answered as a result, its ok true or false,
  // or "busy" for a root that could not be asked.
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

// What was asked of a place, until it has reached the manager: *end* ends one that has not. A letting-go has no
// end, and has reached it once it is answered.
interface Asked {
  reached: Promise<void>;
  end?(): void;
}
const reachedAll = (asked: Asked[]) => Promise.all(asked.map(({ reached }) => reached));

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
  // What each device's browser may open, by its key: told to each manager as it starts.
  private readonly forwarded = new Map<string, Array<[number, string]>>();
  // What was asked of each place, by its key, and has not reached the manager yet, with each letting-go it has
  // not answered: what is asked of the place next is sent after them, so the manager has a place's requests in
  // the order they were asked, which it keeps to itself (manager.ts, VmManager.unplace).
  private readonly turns = new Map<string, Set<Asked>>();
  /** Where a browser's proxy knocks for a connection into a chat's sandbox: the manager's door, there while a guest runs. */
  readonly door: string;

  constructor(private readonly options: VmClientOptions) {
    this.door = join(options.vm.run, DOOR);
  }

  /**
   * One process operation of a root's, in the guest. A cancel is answered at once; the
   * manager is told. One that comes before the VM can boot, its image still downloading,
   * waits for it, and one that comes while a manager that went backs off waits for it,
   * until it is cancelled or the VM is stopped. Never rejects.
   */
  perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    return this.request(operation.id, { type: "op", operation }, signal);
  }

  /**
   * Null once *place* is in the guest, booted for it if none runs: a folder's history and the
   * folder itself, for the agent's own git. Otherwise the answer that says why not, as the manager
   * gave it (manager.ts, VmManager.place). Waits and cancels as an operation does, in its place's
   * turn, and its manager's answer is bounded, as a request's to the place's history is. Never rejects.
   */
  async place(place: Place, signal: AbortSignal): Promise<Outcome | null> {
    if (!whole(place)) return NOT_A_PLACE;
    const id = `place-${randomUUID()}`;
    const outcome = await this.ofPlace(place.key, id, { type: "place", id, place }, signal, PLACE_MS);
    if (!("ok" in outcome)) return outcome;
    // A place added is answered null, and nothing else is.
    return outcome.ok === null ? null : REFUSED;
  }

  /**
   * One request to a folder's history: git in the guest, its place added first. The answer is the
   * manager's (manager.ts, VmManager.history): the guest's, checked in the manager's process before
   * it comes to this one, or why there is none. It waits as an operation does, and is sent in the
   * order it was asked of its place: after a letting-go of the place asked before it has been
   * answered, and before one asked after it, which ends it if it has not reached the manager once
   * a place's time has passed. A cancel is answered at once, and stops its git. A manager that has
   * not answered once it is past its own bounds is killed, as at a teardown, and the request is
   * answered as stopped by the sandbox. Never rejects.
   */
  history(request: HistoryRequest, signal: AbortSignal): Promise<Outcome> {
    if (!named(request)) return Promise.resolve(NOT_A_REQUEST);
    if (!whole(request.place)) return Promise.resolve(NOT_A_PLACE);
    const id = `history-${randomUUID()}`;
    return this.ofPlace(request.place.key, id, { type: "history", id, request }, signal, HISTORY_MS);
  }

  // *message*, sent under *id* in the turn of the place of *key*: once what was asked of the place before it has
  // reached the manager, and a letting-go among that has been answered. Noted until it has reached the manager
  // itself; a letting-go asked meanwhile ends it once that has waited a place's time, and it is answered as let
  // go, with nothing sent. *ms* bounds its manager's answer.
  private async ofPlace(
    key: string, id: string, message: Extract<ToManager, { type: "place" | "history" }>, signal: AbortSignal, ms: (waits: PlaceWaits) => number,
  ): Promise<Outcome> {
    const overtaken = new AbortController();
    const ended = AbortSignal.any([signal, overtaken.signal]);
    let unsent = true;
    let reach = () => {};
    const reached = new Promise<void>((resolve) => {
      reach = () => {
        unsent = false;
        resolve();
      };
    });
    const before = this.turn(key, { reached, end: () => void (unsent && overtaken.abort()) });
    const answered = (async () => {
      // The app's quit ends the wait too, and the request then answers it.
      await Promise.race([reachedAll(before), aborted(AbortSignal.any([ended, this.halted.signal]))]);
      return ended.aborted ? CANCELLED : this.request(id, message, ended, { ms, sent: reach });
    })();
    // At once, though what says when the VM can boot may not heed a cancel.
    const outcome = await Promise.race([answered, aborted(ended)]);
    reach();
    if (outcome === "aborted" || (outcome === CANCELLED && !signal.aborted)) return signal.aborted ? CANCELLED : LET_GO;
    return outcome;
  }

  // Notes *asked* for the place of *key* until it has reached the manager, and gives what was asked of the place before it.
  private turn(key: string, asked: Asked): Asked[] {
    const noted = this.turns.get(key) ?? new Set();
    const before = [...noted];
    this.turns.set(key, noted.add(asked));
    void asked.reached.then(() => {
      noted.delete(asked);
      if (noted.size === 0 && this.turns.get(key) === noted) this.turns.delete(key);
    });
    return before;
  }

  /**
   * *place* leaves the guest, if a manager runs: no thread works on its folder any more, and a guest
   * that holds nothing else stops. True once it has; false where the guest holds no place for that
   * folder and history, as when no manager runs: none is started to ask, and what a manager that
   * went held went with it. It is sent after what was asked of the place before it: what has not
   * reached the manager once a place's time has passed is ended, and answered as let go, as the
   * manager ends what it has not answered by then. A manager that has not answered once it is past
   * its own bounds is killed, as at a teardown. Never rejects.
   */
  unplace(place: Place): Promise<boolean> {
    if (!whole(place)) return Promise.resolve(false);
    let reach = () => {};
    const reached = new Promise<void>((resolve) => {
      reach = resolve;
    });
    const before = this.turn(place.key, { reached });
    const gone = (async () => {
      // A letting-go asked before this one is answered first, as the manager would have it: what was asked between
      // the two has its turn then, and a place's time from then.
      await reachedAll(before.filter(({ end }) => !end));
      const sent = reachedAll(before);
      if (before.length > 0 && (await Promise.race([sent, late(this.waits.setupMs)])) === "late") {
        for (const asked of before) asked.end?.();
        await sent;
      }
      const manager = this.manager;
      if (!manager || this.stopping) return false;
      const id = `unplace-${randomUUID()}`;
      return new Promise<boolean>((resolve) => {
        const bounded = this.bounded(manager, UNPLACE_MS);
        this.pending.set(id, (outcome) => {
          bounded();
          this.pending.delete(id);
          const answer = outcomeOf(outcome);
          resolve(answer !== null && "ok" in answer && answer.ok === true);
        });
        manager.send({ type: "unplace", id, place });
      });
    })();
    void gone.then(reach);
    return gone;
  }

  // The manager's own bounds on a place: its options', else those of the guest as its last boot ran.
  private get waits(): PlaceWaits {
    const table = WAITS[this.emulated ? "emulated" : "kvm"];
    const { setupMs = table.setupMs, shareMs = table.shareMs, historyMs = table.historyMs, powerOffMs = WAITS.emulated.powerOffMs } = this.options.vm;
    return { setupMs, shareMs, historyMs, bootMs: MOST_MS + 2 * WAITS.emulated.helloMs + powerOffMs };
  }

  // Kills *manager* once it has had *ms* of its own bounds and the returned function has not been called: one that
  // has not answered by then is wedged, and its guest goes with it, as at a teardown. What it was asked is then
  // answered as all it was doing is, at its exit.
  private bounded(manager: ManagerProcess, ms: (waits: PlaceWaits) => number): () => void {
    const asked = performance.now();
    let timer: NodeJS.Timeout | undefined;
    const look = () => {
      // By the guest's waits as they are now: a boot may have said since that it runs emulated.
      const left = asked + ms(this.waits) - performance.now();
      if (left <= 0) return manager.kill();
      timer = setTimeout(look, left);
      timer.unref();
    };
    look();
    return () => clearTimeout(timer);
  }

  // *message*, sent to the manager under *id* once the VM can boot and a manager runs, and its answer. What is
  // asked of a place (*own*) is bounded, tells when the manager has it, and takes no answer that is none.
  private async request(
    id: string, message: Extract<ToManager, { type: "op" | "place" | "history" }>, signal: AbortSignal,
    own?: { ms: (waits: PlaceWaits) => number; sent(): void },
  ): Promise<Outcome> {
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
      const bounded = own ? this.bounded(manager, own.ms) : () => {};
      const answer = (outcome: Outcome) => {
        bounded();
        signal.removeEventListener("abort", cancel);
        this.pending.delete(id);
        resolve(own ? (outcomeOf(outcome) ?? REFUSED) : outcome);
      };
      const cancel = () => {
        manager.send({ type: "cancel", id });
        answer(CANCELLED);
      };
      this.pending.set(id, answer);
      signal.addEventListener("abort", cancel, { once: true });
      manager.send(message);
      own?.sent();
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
   * What the browser of the device that knocks with *key* may open from now on: each port of a chat's own
   * servers, with the chat's root. None forgets the key. A manager that starts later is told too, and
   * none is started to be told.
   */
  forwards(key: string, ports: Array<[number, string]>): void {
    if (ports.length === 0) this.forwarded.delete(key);
    else this.forwarded.set(key, ports);
    if (!this.stopping) this.manager?.send({ type: "forwards", key, ports });
  }

  /**
   * Whether something in *root* listens on *port* of its own loopback now. False where no manager
   * runs, which none is started to ask, and when it does not say within the agent's own bound; "busy"
   * for a root that takes no more of the browser's connections now, and so could not be asked. Never rejects.
   */
  listening(root: string, port: number): Promise<boolean | "busy"> {
    const manager = this.manager;
    if (!manager || this.stopping) return Promise.resolve(false);
    const id = `listening-${(this.probes += 1)}`;
    return new Promise((resolve) => {
      const timer = setTimeout(() => answer({ ok: false }), REACH_MS + 5_000);
      const answer = (outcome: Outcome) => {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve("ok" in outcome && (outcome.ok === true || outcome.ok === "busy") ? outcome.ok : false);
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
      // What is no message at all is none of a manager's.
      if (typeof message !== "object" || message === null) return;
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
    for (const [key, ports] of this.forwarded) manager.send({ type: "forwards", key, ports });
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
