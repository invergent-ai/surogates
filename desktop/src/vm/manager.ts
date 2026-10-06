// The VM manager (spec, Section 11): one guest for every chat of this app, booted
// at the first process operation of any root, with each root's folder shared into
// it when that root first needs the guest. What differs by OS is behind one
// interface, VmBackend, with a backend per OS (linux.ts); everything here is the
// same on every OS: hello, the keepalive, the roots' set-up and teardown, and what
// a lost guest was running.

import { rmSync } from "node:fs";
import { lstat, realpath } from "node:fs/promises";
import type { Duplex } from "node:stream";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { FromAgent, HostUser } from "../guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { ControlLink, type Request } from "./control.js";
import { bootLinux } from "./linux.js";
import type { Disks } from "./qemu.js";

const HELLO_MS = 15_000;
// A ping every PING_MS; MISSED in a row unanswered is a hung guest.
const PING_MS = 10_000;
const MISSED = 3;
// The agent's own bounds on a setup, 5 s for the share and 5 s for the runner, fit inside it.
const SETUP_MS = 15_000;
// The agent gives its roots uids from here up.
const FIRST_UID = 10_000;

export interface VmOptions extends Disks {
  run: string; // the backend's runtime folder, this user's own: on Linux, the sockets and pidfiles
  console: string; // the guest's console log
  user: HostUser; // whom the roots run for: the name and home they see
  cpus?: number;
  pingMs?: number;
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
  /** Settles once the VM has gone, however it went, with the end of what its hypervisor said, or "". */
  readonly exited: Promise<string>;
  /**
   * *folder* shared into the running guest, owned there by *uid*, its root's guest
   * uid, and on the host by the host user. Resolves with the share's tag, which the
   * agent mounts, or rejects with why not. A share whose server goes takes the VM with it.
   */
  share(folder: string, uid: number): Promise<string>;
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

// A root's folder as its binding holds it: the path, and its identity when it was bound.
export interface Folder {
  path: string;
  dev: number;
  ino: number;
}

export interface VmOperation {
  id: string;
  root: string;
  folder: Folder;
  kind: string;
  args: Record<string, unknown>;
}

export const unavailable = (why: string): Outcome => ({ error: { type: "unavailable", message: `This computer's sandbox ${why}` } });
const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));

// The folder is not the one its chat was bound to: its share would serve whatever is at the path now.
class FolderGone extends Error {}

// Settles once *signal* aborts.
const aborted = (signal: AbortSignal) => new Promise<"aborted">((resolve) => {
  if (signal.aborted) resolve("aborted");
  else signal.addEventListener("abort", () => resolve("aborted"), { once: true });
});

interface Root {
  share: Promise<string>; // its share's tag, once added
  setup: Promise<Outcome | null> | null; // null once set up, or why not
}

// One boot of the guest, until it goes.
export class Guest {
  readonly gone: Promise<void>;
  private leave: () => void = () => {};
  private left = false;
  private readonly roots = new Map<string, Root>();
  private keepalive: NodeJS.Timeout | undefined;

  private constructor(
    options: VmOptions,
    private readonly vm: VmBackend,
    private readonly control: ControlLink,
    // When the boot began (performance.now()), and how long after it the agent said hello.
    readonly launched: number,
    readonly helloMs: number,
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
    let missed = 0;
    let waiting = false;
    this.keepalive = setInterval(() => {
      missed = waiting ? missed + 1 : 0;
      if (missed >= MISSED) return this.lose();
      if (waiting) return;
      waiting = true;
      void control.request({ type: "ping" }).then((pong) => {
        if (pong) waiting = false;
      });
    }, options.pingMs ?? PING_MS);
    this.keepalive.unref();
  }

  /**
   * The guest *boot* starts, once its agent has said hello. Rejects with why it did
   * not start, its hypervisor's words included. *signal* stops a boot under way.
   */
  static async boot(boot: BootVm, options: VmOptions, signal?: AbortSignal): Promise<Guest> {
    const launched = performance.now();
    const deadline = launched + HELLO_MS;
    const vm = await boot(options, signal, deadline);
    const halt = () => void vm.kill();
    signal?.addEventListener("abort", halt, { once: true });
    try {
      const control = await ControlLink.open(vm.control, options.user, deadline, vm.exited);
      return new Guest(options, vm, control, launched, performance.now() - launched);
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

  op(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    return this.control.op(root, kind, args, signal);
  }

  /** Null once *root* is set up, its folder added; otherwise the answer that says why not, and the next asks again. */
  ready(root: string, folder: Folder): Promise<Outcome | null> {
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
      let tag: string;
      try {
        tag = await entry.share;
      } catch (error) {
        entry.setup = null;
        return error instanceof FolderGone ? FOLDER_UNAVAILABLE : unavailable(`could not add this chat's folder: ${describe(error)}`);
      }
      const answer = await this.request({ type: "setup", root, folder: folder.path, tag }, SETUP_MS);
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
   * and the agent has given the root's guest uid, which the share maps the host
   * user to. Resolves with its share's tag.
   */
  async share(root: string, folder: Folder): Promise<string> {
    const found = await lstat(folder.path).catch(() => null);
    const real = await realpath(folder.path).catch(() => null);
    if (!found?.isDirectory() || found.dev !== folder.dev || found.ino !== folder.ino || real !== folder.path) throw new FolderGone();
    const given = await this.request({ type: "uid", root });
    // A uid the agent cannot have given goes into no share.
    const uid = given?.type === "done" ? given.uid : undefined;
    if (uid === undefined || !Number.isInteger(uid) || uid < FIRST_UID) {
      throw new Error(given?.type === "failed" ? given.message : "the guest gave no uid a root can have");
    }
    return this.vm.share(folder.path, uid);
  }

  /** Everything of *root* ends in the guest, its share left in place; its next operation sets it up again. */
  async teardown(root: string): Promise<void> {
    const entry = this.roots.get(root);
    if (!entry) return;
    // A setup under way lands first: the teardown then ends what it set up.
    await entry.setup;
    entry.setup = null;
    // Unanswered: the agent is stuck, and the guest goes, the root's processes with it.
    if (!(await this.request({ type: "teardown", root }, SETUP_MS))) this.lose();
  }

  // Settles once all of the VM has gone.
  stop(): Promise<void> {
    this.lose();
    return this.vm.kill();
  }

  private lose(): void {
    if (this.left) return;
    this.left = true;
    clearInterval(this.keepalive);
    this.control.close();
    void this.vm.kill().catch(() => {});
    this.leave();
  }
}

export class VmManager {
  private guest: Promise<Guest> | null = null;
  private stopping = false;
  // Stops a boot under way when the manager stops.
  private readonly halt = new AbortController();

  constructor(private readonly options: VmOptions, private readonly boot: BootVm | null = bootFor(process.platform)) {}

  /**
   * One process operation of a root's, in the guest, its folder added first. A
   * cancel is answered at once, and a cancel before the folder is added adds
   * nothing. Never rejects.
   */
  async perform(operation: VmOperation, signal: AbortSignal): Promise<Outcome> {
    if (this.stopping) return unavailable("is stopping");
    // No backend for this OS yet: answered as a VM that cannot start is.
    if (!this.boot) return unavailable("is not available on this platform yet");
    let guest: Guest | "aborted";
    try {
      guest = await Promise.race([this.booted(this.boot), aborted(signal)]);
    } catch (error) {
      return unavailable(this.stopping ? "is stopping" : `did not start: ${describe(error)}`);
    }
    if (guest === "aborted") return CANCELLED;
    const failure = await Promise.race([guest.ready(operation.root, operation.folder), aborted(signal)]);
    if (failure === "aborted") return CANCELLED;
    if (failure) return this.stopping ? unavailable("is stopping") : failure;
    return guest.op(operation.root, operation.kind, operation.args, signal);
  }

  /** Everything of *root* ends in the guest, if one runs: its folder is being let go. Never rejects. */
  async teardown(root: string): Promise<void> {
    const guest = await this.guest?.catch(() => null);
    await guest?.teardown(root);
  }

  // Its guest's runtime folder goes with it.
  async stop(): Promise<void> {
    this.stopping = true;
    this.halt.abort();
    const guest = await this.guest?.catch(() => null);
    await guest?.stop();
    rmSync(this.options.run, { recursive: true, force: true });
  }

  // The guest that runs, or a new one: a guest that went, or did not start, is booted again by the next operation.
  private booted(boot: BootVm): Promise<Guest> {
    if (this.guest) return this.guest;
    const booting = Guest.boot(boot, this.options, this.halt.signal);
    this.guest = booting;
    booting.then((guest) => guest.gone, () => {}).finally(() => {
      if (this.guest === booting) this.guest = null;
    });
    return booting;
  }
}
