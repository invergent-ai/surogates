// The VM manager (spec, Section 11): one guest for every chat of this app, booted
// at the first process operation of any root, with each root's folder shared into
// it when that root first needs the guest. What differs by OS is behind one
// interface, VmBackend, with a backend per OS (linux.ts); everything here is the
// same on every OS: hello, the keepalive, the roots' set-up and teardown, the host
// proxy on the guest's net port, and what a lost guest was running.

import { readFileSync, renameSync, rmSync } from "node:fs";
import type { Duplex } from "node:stream";
import { setTimeout as wait } from "node:timers/promises";
import { Worker } from "node:worker_threads";

import { confirmedFolder } from "../binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED, timedOut } from "../guest/command.js";
import type { ProcessHandle } from "../guest/processes.js";
import { type FromAgent, HELD, type HostUser, type Share } from "../guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { Backoff } from "./backoff.js";
import { ControlLink, type Request } from "./control.js";
import { bootLinux } from "./linux.js";
import { type Egress, NetProxy, withNotice } from "./proxy.js";
import type { Disks } from "./qemu.js";

// A ping every PING_MS, with KVM or emulated.
const PING_MS = 10_000;
// Section 11's waits for a boot, with KVM and emulated, where everything in the guest is slower:
// - helloMs, from the launch to the agent's hello;
// - missed, the pings in a row unanswered that make a hung guest;
// - powerOffMs, from the shutdown asked to the VM's exit, past which it is ended;
// - setupMs, a root's set-up, and the agent's answer to a teardown. The agent's own bounds
//   fit inside it: its share's mount, what the root ran before to end, and its runner's start;
// - shareMs, a share's hot-add, from the agent's uid to the folder in the guest, and its removal.
export const WAITS = {
  kvm: { helloMs: 15_000, missed: 3, powerOffMs: 5_000, setupMs: 15_000, shareMs: 15_000 },
  emulated: { helloMs: 120_000, missed: 9, powerOffMs: 30_000, setupMs: 90_000, shareMs: 90_000 },
} as const;
// The agent gives its roots uids from here up.
const FIRST_UID = 10_000;
// This computer's wall clock past its monotonic one by more than this since the last look: it slept.
const SLEPT_MS = 2_000;
const MAX_TIMER_MS = 2 ** 31 - 1;

// Why a boot runs emulated, under QEMU's TCG, rather than with KVM (spec, Section 11): this
// computer has no hardware virtualization (no-kvm); its user cannot open it, and is in its group
// from the next login on (relogin) or is not (no-access); or QEMU could not use it (kvm-failed).
export type Emulated = "no-kvm" | "no-access" | "relogin" | "kvm-failed";

export interface VmOptions extends Disks {
  run: string; // the backend's runtime folder, this user's own: on Linux, the sockets and pidfiles
  console: string; // the guest's console log
  user: HostUser; // whom the roots run for: the name and home they see
  kvm?: string; // the device KVM is opened from: /dev/kvm
  cpus?: number;
  pingMs?: number;
  shareMs?: number;
  setupMs?: number;
  powerOffMs?: number;
}

/**
 * One booted VM, as its OS's backend runs it: the VM layer's one interface. Behind
 * it are booting and ending the VM, its control channel, sharing a folder into the
 * running guest, its exit, the sweep of what a manager that died left, and where
 * its runtime files live. The VM manager, VmClient, VmExecutor and the guest's
 * protocol above it know no backend.
 */
export interface VmBackend {
  /** The guest's control port, ai.surogate.control, as a byte stream. The agent's hello comes on it. */
  readonly control: Duplex;
  /**
   * The guest's network port, ai.surogate.net, as a byte stream: the guest's only way out.
   * The agent opens one HTTP/2 session on it, a CONNECT stream for each connection its
   * roots' commands make, which the host proxy serves (proxy.ts). Linux's is a
   * virtio-serial port; a backend that has a socket per connection to give, as hvsock or
   * vsock, gives one connection for this.
   */
  readonly net: Duplex;
  /** Settles once the VM has gone, however it went, with the end of what its hypervisor said, or "". */
  readonly exited: Promise<string>;
  /** Null with the OS's hardware virtualization, else why the VM runs emulated. */
  readonly emulated: Emulated | null;
  /**
   * *folder* shared into the running guest for the root whose guest uid is *uid*.
   * Resolves with how the agent mounts it, whose kind also says who maps the
   * folder's owner to *uid* (protocol.ts, Share), or rejects with why not, by
   * *deadline* (performance.now()). A share whose server goes takes the VM with it,
   * and so does one that leaves the VM unable to share again: it rejects once the VM has gone.
   */
  share(folder: string, uid: number, deadline: number): Promise<Share>;
  /**
   * *share*, as share gave it, out of the running guest once the guest has let it go, and
   * its folder served no more: its place takes the next share. Rejects with why not by
   * *deadline* (performance.now()), once the VM has gone: a guest that does not let a
   * folder go is given no other. The agent's unmount is lazy, so the guest may still hold
   * the share when this is asked, until the deadline. Whether a tag comes again is the
   * backend's: Linux's never does within a boot, and one whose places are fixed may.
   */
  unshare(share: Share, deadline: number): Promise<void>;
  /** Ends the VM at once; settles once all of it has gone. Its runtime files go at the next boot, or with the manager. */
  kill(): Promise<void>;
}

/**
 * Boots a VM on *options*. Resolves once its control channel is open, or rejects
 * with why it did not start, in its hypervisor's words. *signal* ends a boot under
 * way, and *deadline* (performance.now()) bounds it.
 */
export type BootVm = (options: VmOptions, signal: AbortSignal | undefined, deadline: number) => Promise<VmBackend>;

/** The backend for *platform*: Linux's, or null on an OS whose backend is not built yet. */
export function bootFor(platform: NodeJS.Platform): BootVm | null {
  return platform === "linux" ? bootLinux : null;
}

// A root's background processes in the guest, each time they change: the handles to
// keep and how many live, or gone, when the guest went and they with it.
export type ProcessesChange = { handles: ProcessHandle[]; live: number } | { gone: true };
type Told = (root: string, change: ProcessesChange) => void;

// A root's folder as its binding holds it: the path, and its identity when it was
// bound, in the boot it was bound in ("", or none, when that could not be read).
export interface Folder {
  path: string;
  dev: number;
  ino: number;
  boot?: string;
}

export interface VmOperation {
  id: string;
  root: string;
  folder: Folder;
  kind: string;
  args: Record<string, unknown>;
  // The handles the host keeps of the root's background processes: a root new to the guest answers for them.
  ended?: ProcessHandle[];
}

export const unavailable = (why: string): Outcome => ({ error: { type: "unavailable", message: `This computer's sandbox ${why}` } });
// What the agent is told once a chat, at the end of its first run's output, while the guest runs emulated.
export const EMULATED_NOTICE = "This computer runs commands in an emulated sandbox, about 5 to 20 times slower than usual. Give long commands more time.";
const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));

// The folder is not the one its chat was bound to: its share would serve whatever is at the path now.
class FolderGone extends Error {}

// Settles with "late" once *ms* pass, its timer not holding the process.
const late = (ms: number) => new Promise<"late">((resolve) => {
  setTimeout(() => resolve("late"), Math.max(0, ms)).unref();
});

// Settles once *signal* aborts.
const aborted = (signal: AbortSignal) => new Promise<"aborted">((resolve) => {
  if (signal.aborted) resolve("aborted");
  else signal.addEventListener("abort", () => resolve("aborted"), { once: true });
});

interface Root {
  share: Promise<Share>; // how the agent mounts its folder, once added
  setup: Promise<Outcome | null> | null; // null once set up, or why not
}

// What a look at a folder found: its identity, and its real path.
interface Looked {
  found: { directory: boolean; dev: number; ino: number } | null;
  real: string | null;
}

// A folder's look, in a thread of its own (Worker): on a mount that does not answer it
// never returns, and in libuv's pool it would hold one of the four threads the host
// proxy's lookups need too. A look still waiting is the next one's, so a folder that
// does not answer holds one thread, however often it is asked.
const LOOK = `
const { parentPort, workerData } = require("node:worker_threads");
const { lstatSync, realpathSync } = require("node:fs");
let found = null;
let real = null;
try {
  const stat = lstatSync(workerData);
  found = { directory: stat.isDirectory(), dev: stat.dev, ino: stat.ino };
  real = realpathSync.native(workerData);
} catch {}
parentPort.postMessage({ found, real });
`;
const looking = new Map<string, Promise<Looked>>();

function look(path: string): Promise<Looked> {
  const known = looking.get(path);
  if (known) return known;
  const looked = new Promise<Looked>((resolve) => {
    const worker = new Worker(LOOK, { eval: true, workerData: path });
    // A look that never returns keeps no process alive.
    worker.unref();
    worker.once("message", (found: Looked) => resolve(found));
    worker.once("error", () => resolve({ found: null, real: null }));
  });
  looking.set(path, looked);
  void looked.then(() => looking.delete(path));
  return looked;
}

// Without an Egress, every destination off the package hosts is refused.
const REFUSING: Egress = { ask: () => Promise.resolve(false) };

// One boot of the guest, until it goes.
export class Guest {
  // Settles once the guest has gone and all of its VM with it, so a next boot finds its disks free.
  readonly gone: Promise<void>;
  private leave: () => void = () => {};
  private left = false;
  private readonly roots = new Map<string, Root>();
  private keepalive: NodeJS.Timeout | undefined;
  // Pings in a row the agent has not answered.
  private missed = 0;
  readonly emulated: Emulated | null;
  // This computer's wall clock less its monotonic one, at the last look: the sleep is what it grew by since.
  private offset = Date.now() - performance.now();
  private readonly shareMs: number;
  private readonly setupMs: number;
  private readonly powerOffMs: number;
  private readonly proxy: NetProxy;
  // Its own stop, once asked: a guest that goes without one was lost.
  private stopping: Promise<void> | null = null;

  private constructor(
    options: VmOptions,
    private readonly vm: VmBackend,
    private readonly control: ControlLink,
    // When the boot began (performance.now()), and how long after it the agent said hello.
    readonly launched: number,
    readonly helloMs: number,
    private readonly told: Told,
    egress: Egress,
  ) {
    this.gone = new Promise((resolve) => {
      this.leave = resolve;
    });
    void vm.exited.then(() => this.lose());
    void control.closed.then(() => this.lose());
    control.onLost((root) => {
      const entry = this.roots.get(root);
      if (entry) entry.setup = null;
    });
    control.onHandles((root, handles, live) => told(root, { handles, live }));
    this.emulated = vm.emulated;
    const waits = WAITS[vm.emulated ? "emulated" : "kvm"];
    let waiting = false;
    this.keepalive = setInterval(() => {
      // A tick may come before the wake's resume: the sleep is told whichever comes first.
      this.wake();
      this.missed = waiting ? this.missed + 1 : 0;
      if (this.missed >= waits.missed) return this.lose();
      if (waiting) return;
      waiting = true;
      void control.request({ type: "ping" }).then((pong) => {
        if (pong) waiting = false;
      });
    }, options.pingMs ?? PING_MS);
    this.keepalive.unref();
    this.shareMs = options.shareMs ?? waits.shareMs;
    this.setupMs = options.setupMs ?? waits.setupMs;
    this.powerOffMs = options.powerOffMs ?? waits.powerOffMs;
    // Each connection a root's command makes, judged with the root the agent named.
    this.proxy = new NetProxy(vm.net, { egress });
  }

  /**
   * The guest *boot* starts, once its agent has said hello. Rejects with why it did
   * not start, its hypervisor's words included. *signal* stops a boot under way.
   */
  static async boot(boot: BootVm, options: VmOptions, signal?: AbortSignal, told: Told = () => {}, egress = REFUSING): Promise<Guest> {
    const launched = performance.now();
    // The backend's own part, QEMU's sockets and monitor, is as quick emulated: the guest has not begun.
    const vm = await boot(options, signal, launched + WAITS.kvm.helloMs);
    const halt = () => void vm.kill();
    signal?.addEventListener("abort", halt, { once: true });
    // Stopped between the backend's listener and this one.
    if (signal?.aborted) halt();
    try {
      const deadline = launched + WAITS[vm.emulated ? "emulated" : "kvm"].helloMs;
      const control = await ControlLink.open(vm.control, options.user, deadline, vm.exited);
      return new Guest(options, vm, control, launched, performance.now() - launched, told, egress);
    } catch (error) {
      await vm.kill();
      throw new Error([describe(error), await vm.exited].filter(Boolean).join(": "));
    } finally {
      signal?.removeEventListener("abort", halt);
    }
  }

  request(message: Request, ms?: number): Promise<FromAgent | null> {
    return this.control.request(message, ms);
  }

  // A run that answers carries what the root's connections could not reach since its last;
  // one that answers with an error leaves it for the next.
  //
  // A command's timeout is this computer's to keep, on its monotonic clock, which does not count
  // its sleep: a command it slept through goes on at the wake, whatever the guest's clock did. At
  // the timeout the command is cancelled in the guest, which ends it, and answered as timed out.
  // The guest's own deadline is a backstop past it (guest/root.ts, Backstop).
  async op(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    const { timeout } = args;
    const timed = kind === "run" && typeof timeout === "number" && Number.isFinite(timeout) && timeout > 0;
    const expiry = new AbortController();
    const timer = timed ? setTimeout(() => expiry.abort(), Math.min(timeout * 1000, MAX_TIMER_MS)) : undefined;
    let outcome: Outcome;
    try {
      outcome = await this.control.op(root, kind, args, AbortSignal.any([signal, expiry.signal]));
    } finally {
      clearTimeout(timer);
    }
    if (timed && expiry.signal.aborted && !signal.aborted) outcome = timedOut(timeout);
    if (kind !== "run" || !("ok" in outcome)) return outcome;
    return withNotice(outcome, this.proxy.takeNotice(root));
  }

  /**
   * Null once *root* is set up, its folder added and its processes' *ended* handles
   * given; otherwise the answer that says why not, and the next asks again.
   */
  ready(root: string, folder: Folder, ended: ProcessHandle[] = []): Promise<Outcome | null> {
    let known = this.roots.get(root);
    if (!known) {
      const entry: Root = { share: this.share(root, folder), setup: null };
      // A share that could not be added is tried again by the next operation.
      entry.share.catch(() => {
        if (this.roots.get(root) === entry) this.roots.delete(root);
      });
      this.roots.set(root, entry);
      known = entry;
    }
    const entry = known;
    entry.setup ??= (async () => {
      let share: Share;
      try {
        share = await entry.share;
      } catch (error) {
        entry.setup = null;
        // The guest went while the folder was being added: as what it ran.
        if (this.left) return SANDBOX_STOPPED;
        return error instanceof FolderGone ? FOLDER_UNAVAILABLE : unavailable(`could not add this chat's folder: ${describe(error)}`);
      }
      // A namespace of its own, with nothing met yet: a connection of the one torn down can
      // land after its teardown.
      this.proxy.forget(root);
      const answer = await this.request({ type: "setup", root, folder: folder.path, share, ended }, this.setupMs);
      if (answer?.type === "done") return null;
      entry.setup = null;
      if (answer?.type === "failed") return unavailable(`could not set up this chat: ${answer.message}`);
      // No answer in time: the agent is stuck, and the guest goes with it.
      this.lose();
      return SANDBOX_STOPPED;
    })();
    return entry.setup;
  }

  /**
   * *folder*, shared into the guest for *root* once its identity is checked again
   * and the agent has given the root's guest uid. Resolves with how the agent mounts
   * it. A uid not given within shareMs stops the guest, as a setup with no answer
   * does; the backend ends a VM that cannot share any more, and the guest goes with it.
   */
  async share(root: string, folder: Folder): Promise<Share> {
    const deadline = performance.now() + this.shareMs;
    // On a mount that does not answer, as a dead network or FUSE one, the look never
    // returns, and Node cannot cancel it: it is given up on at the deadline, in a thread of its own.
    const looked = await Promise.race([look(folder.path), late(deadline - performance.now())]);
    if (looked === "late") throw new Error(`it did not answer within ${this.shareMs / 1000} s`);
    const { found, real } = looked;
    if (!found?.directory || !confirmedFolder(folder, found) || real !== folder.path) throw new FolderGone();
    const given = await this.request({ type: "uid", root }, Math.max(0, deadline - performance.now()));
    if (!given) {
      this.lose();
      throw new Error("the guest gave no uid in time");
    }
    // A uid the agent cannot have given goes into no share.
    const uid = given.type === "done" ? given.uid : undefined;
    if (uid === undefined || !Number.isInteger(uid) || uid < FIRST_UID) {
      throw new Error(given.type === "failed" ? given.message : "the guest gave no uid a root can have");
    }
    return this.vm.share(folder.path, uid, deadline);
  }

  /**
   * Everything of *root* ends in the guest, then its folder leaves it; its next operation
   * shares the folder and sets it up again. A guest that does not answer, or does not let
   * the folder go, is lost, the root's processes with it.
   */
  async teardown(root: string): Promise<void> {
    const entry = this.roots.get(root);
    if (!entry) return;
    // A setup under way lands first: the teardown then ends what it set up. One that
    // does not land within setupMs loses the guest, the root's processes with it.
    if ((await Promise.race([entry.setup, late(this.setupMs)])) === "late") return this.lose();
    entry.setup = null;
    // One that could not be added has left already.
    const share = await entry.share.catch(() => null);
    if (this.roots.get(root) === entry) this.roots.delete(root);
    this.proxy.forget(root);
    if (!share) return;
    const answer = await this.request({ type: "teardown", root, share }, this.setupMs);
    // Unanswered: the agent is stuck, and the guest goes, the root's processes with it.
    if (!answer) return this.lose();
    // What of the root would not end, waiting on a share that stalled, still holds the share,
    // and its removal would wait on the guest for good: it stays, its place taken, until the
    // VM stops. The root's next setup gets a share of its own. Any other failure lets it go.
    if (answer.type === "failed" && answer.message === HELD) return;
    await this.vm.unshare(share, performance.now() + this.shareMs).catch(() => this.lose());
  }

  /**
   * The guest's own stop, the spike's sync before kill: its agent ends every root, writes
   * the sessions disk out and lets it go, and powers the guest off. A VM still running
   * powerOffMs after the ask is ended. Settles once all of it has gone.
   */
  stop(): Promise<void> {
    this.stopping ??= (async () => {
      if (!this.left) {
        // A guest powering off answers no ping.
        clearInterval(this.keepalive);
        void this.control.request({ type: "shutdown" });
        await Promise.race([this.vm.exited, late(this.powerOffMs)]);
      }
      this.lose();
      await this.vm.kill();
    })();
    return this.stopping;
  }

  // Whether it has gone, or is stopping, though all of its VM may not have gone yet.
  get ended(): boolean {
    return this.left || this.stopping !== null;
  }

  // Whether it went without its own stop: it crashed, or was ended as stuck.
  get lost(): boolean {
    return this.left && this.stopping === null;
  }

  // Whether it holds no root, and runs on: nothing of any chat is in it.
  get idle(): boolean {
    return this.roots.size === 0 && !this.ended;
  }

  /**
   * The computer woke: the guest is told the time and how long the computer slept, and the
   * pings it missed meanwhile are not held against it.
   */
  resume(): void {
    this.missed = 0;
    this.wake(true);
  }

  // How long this computer slept since the last look, from its own two clocks, told the guest
  // with the time when it slept, or when *asked* (a wake).
  private wake(asked = false): void {
    const offset = Date.now() - performance.now();
    const slept = Math.max(0, offset - this.offset);
    this.offset = offset;
    if (this.ended || (!asked && slept < SLEPT_MS)) return;
    void this.request({ type: "time", now: Date.now(), slept: slept < SLEPT_MS ? 0 : slept }, this.setupMs);
  }

  private lose(): void {
    if (this.left) return;
    this.left = true;
    clearInterval(this.keepalive);
    this.control.close();
    this.proxy.close();
    // Before what waited on it is answered: the next operation finds them ended.
    for (const root of this.roots.keys()) this.told(root, { gone: true });
    void this.vm.kill().catch(() => {}).then(this.leave);
  }
}

// The guest's init says so on its console when it cannot check the sessions disk (vm/init).
const UNCHECKED = "surogate: e2fsck could not check the sessions disk";

export class VmManager {
  private guest: Promise<Guest> | null = null;
  private stopping = false;
  // Stops a boot under way when the manager stops.
  private readonly halt = new AbortController();
  // Each boot waits out the last failure's backoff; what a boot that failed said, while it lasts.
  private readonly backoff = new Backoff();
  private failed: string | null = null;
  // Operations and teardowns under way: a guest with none, and no root, stops.
  private working = 0;
  // The chats told that their commands run emulated: once a chat, whichever boot it was in.
  private readonly noticed = new Set<string>();

  // *told*: each change of a root's processes in its guests. *egress*: who lets a root's commands reach past the package hosts.
  constructor(
    private readonly options: VmOptions,
    private readonly boot: BootVm | null = bootFor(process.platform),
    private readonly told: Told = () => {},
    private readonly egress: Egress = REFUSING,
  ) {}

  /**
   * One process operation of a root's, in the guest, its folder added first. A
   * cancel is answered at once, and a cancel before the folder is added adds
   * nothing. While a boot that failed backs off, it is answered with that boot's failure
   * at once. Never rejects.
   */
  async perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (this.stopping) return unavailable("is stopping");
    // No backend for this OS yet: answered as a VM that cannot start is.
    if (!this.boot) return unavailable("is not available on this platform yet");
    if (this.failed && this.backoff.wait > 0 && !this.guest) return unavailable(`did not start: ${this.failed}`);
    this.working += 1;
    try {
      let guest: Guest | "aborted";
      try {
        guest = await Promise.race([this.booted(this.boot), aborted(signal)]);
      } catch (error) {
        return unavailable(this.stopping ? "is stopping" : `did not start: ${describe(error)}`);
      }
      if (guest === "aborted") return CANCELLED;
      const failure = await Promise.race([guest.ready(operation.root, operation.folder, operation.ended), aborted(signal)]);
      if (failure === "aborted") return CANCELLED;
      if (failure) return this.stopping ? unavailable("is stopping") : failure;
      const outcome = await guest.op(operation.root, operation.kind, operation.args, signal);
      if (!guest.emulated || operation.kind !== "run" || !("ok" in outcome) || this.noticed.has(operation.root)) return outcome;
      this.noticed.add(operation.root);
      return withNotice(outcome, EMULATED_NOTICE);
    } finally {
      this.done();
    }
  }

  /** Everything of *root* ends in the guest, if one runs: its folder is being let go. Never rejects. */
  async teardown(root: string): Promise<void> {
    this.working += 1;
    try {
      const guest = await this.guest?.catch(() => null);
      await guest?.teardown(root);
    } finally {
      this.done();
    }
  }

  /** The computer woke: the guest that runs is told so. */
  resume(): void {
    void this.guest?.then((guest) => guest.resume(), () => {});
  }

  // Its guest's runtime folder goes with it.
  async stop(): Promise<void> {
    this.stopping = true;
    this.halt.abort();
    const guest = await this.guest?.catch(() => null);
    await guest?.stop();
    rmSync(this.options.run, { recursive: true, force: true });
  }

  // Once nothing is under way: a guest that holds no root, its last one's folder let go or
  // none ever added, stops (Section 11, Lifecycle). The next operation boots another.
  private done(): void {
    this.working -= 1;
    if (this.working > 0 || this.stopping) return;
    void this.guest?.then((guest) => {
      if (this.working === 0 && guest.idle) void guest.stop();
    }, () => {});
  }

  // The guest that runs, or a new one: a guest that went, or did not start, is booted
  // again by the next operation, once all of the one that went has gone.
  private booted(boot: BootVm): Promise<Guest> {
    const current = (this.guest ??= this.start(boot));
    return current.then(async (guest) => {
      if (!guest.ended) return guest;
      await guest.gone;
      if (this.guest === current) this.guest = null;
      return this.booted(boot);
    });
  }

  // A boot, once the last failure's backoff has passed. One whose guest could not check its
  // sessions disk boots again on a new one: the old one is kept beside it, the next to fail
  // its check taking its place, as the homes and caches it holds can be lost.
  private start(boot: BootVm): Promise<Guest> {
    const booting = (async () => {
      await wait(this.backoff.wait, undefined, { signal: this.halt.signal });
      // What the console says is then this boot's alone.
      rmSync(this.options.console, { force: true });
      try {
        return await Guest.boot(boot, this.options, this.halt.signal, this.told, this.egress);
      } catch (error) {
        if (this.stopping || !this.unchecked()) throw error;
        renameSync(this.options.sessions, `${this.options.sessions}.unchecked`);
        return Guest.boot(boot, this.options, this.halt.signal, this.told, this.egress);
      }
    })();
    booting.then(async (guest) => {
      this.failed = null;
      this.backoff.up();
      await guest.gone;
      // One that crashed, or stuck, backs off its next boot; one that stopped, idle or asked, does not.
      if (guest.lost) this.backoff.down();
    }, (error: unknown) => {
      if (this.stopping) return;
      this.failed = describe(error);
      this.backoff.down();
    }).finally(() => {
      if (this.guest === booting) this.guest = null;
    });
    return booting;
  }

  // Whether the guest's console says it could not check the sessions disk.
  private unchecked(): boolean {
    try {
      return readFileSync(this.options.console, "utf8").includes(UNCHECKED);
    } catch {
      return false;
    }
  }
}
