// The VM manager (spec, Section 11): one guest for every chat of this app, booted
// at the first process operation of any root, with each root's folder shared into
// it when that root first needs the guest. What differs by OS is behind one
// interface, VmBackend, with a backend per OS (linux.ts); everything here is the
// same on every OS: hello, the keepalive, the roots' set-up and teardown, and what
// a lost guest was running.

import { rmSync } from "node:fs";
import { lstat, realpath } from "node:fs/promises";
import type { Duplex } from "node:stream";

import { BOOT_ID } from "../binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import type { ProcessHandle } from "../guest/processes.js";
import type { FromAgent, HostUser, Share } from "../guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Outcome } from "../link/protocol.js";
import { ControlLink, type Request } from "./control.js";
import { bootLinux } from "./linux.js";
import type { Disks } from "./qemu.js";

const HELLO_MS = 15_000;
// A ping every PING_MS; MISSED in a row unanswered is a hung guest.
const PING_MS = 10_000;
const MISSED = 3;
// The agent's own bounds on a setup fit inside it: 5 s to mount the share, 3 s for
// what the root ran before to end, and 5 s for its runner to start.
const SETUP_MS = 15_000;
// A share's hot-add, from the agent's uid to the folder in the guest (Section 11's timeouts).
const SHARE_MS = 15_000;
// The agent gives its roots uids from here up.
const FIRST_UID = 10_000;

export interface VmOptions extends Disks {
  run: string; // the backend's runtime folder, this user's own: on Linux, the sockets and pidfiles
  console: string; // the guest's console log
  user: HostUser; // whom the roots run for: the name and home they see
  cpus?: number;
  pingMs?: number;
  shareMs?: number;
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
   * *folder* shared into the running guest for the root whose guest uid is *uid*.
   * Resolves with how the agent mounts it, whose kind also says who maps the
   * folder's owner to *uid* (protocol.ts, Share), or rejects with why not, by
   * *deadline* (performance.now()). A share whose server goes takes the VM with it,
   * and so does one that leaves the VM unable to share again: it rejects once the VM has gone.
   */
  share(folder: string, uid: number, deadline: number): Promise<Share>;
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

// One boot of the guest, until it goes.
export class Guest {
  // Settles once the guest has gone and all of its VM with it, so a next boot finds its disks free.
  readonly gone: Promise<void>;
  private leave: () => void = () => {};
  private left = false;
  private readonly roots = new Map<string, Root>();
  private keepalive: NodeJS.Timeout | undefined;
  private readonly shareMs: number;

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
    this.shareMs = options.shareMs ?? SHARE_MS;
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
    // Stopped between the backend's listener and this one.
    if (signal?.aborted) halt();
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

  /**
   * Null once *root* is set up, its folder added, its processes' *ended* handles
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
      const answer = await this.request({ type: "setup", root, folder: folder.path, share, ended }, SETUP_MS);
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
   * it. A uid not given within SHARE_MS stops the guest, as a setup with no answer
   * does; the backend ends a VM that cannot share any more, and the guest goes with it.
   */
  async share(root: string, folder: Folder): Promise<Share> {
    const deadline = performance.now() + this.shareMs;
    // On a mount that does not answer, as a dead network or FUSE one, the look never
    // returns, and Node cannot cancel it: it is given up on at the deadline, its thread still held.
    const looked = await Promise.race([
      (async () => [await lstat(folder.path).catch(() => null), await realpath(folder.path).catch(() => null)] as const)(),
      late(deadline - performance.now()),
    ]);
    if (looked === "late") throw new Error(`it did not answer within ${this.shareMs / 1000} s`);
    const [found, real] = looked;
    // A reboot can renumber the folder's mount: after one, only the inode is compared, as the file host does.
    const rebooted = Boolean(folder.boot) && BOOT_ID !== "" && folder.boot !== BOOT_ID;
    if (!found?.isDirectory() || (!rebooted && found.dev !== folder.dev) || found.ino !== folder.ino || real !== folder.path) throw new FolderGone();
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

  /** Everything of *root* ends in the guest, its share left in place; its next operation sets it up again. */
  async teardown(root: string): Promise<void> {
    const entry = this.roots.get(root);
    if (!entry) return;
    // A setup under way lands first: the teardown then ends what it set up. One that
    // does not land within SETUP_MS loses the guest, the root's processes with it.
    if ((await Promise.race([entry.setup, late(SETUP_MS)])) === "late") return this.lose();
    entry.setup = null;
    // Unanswered: the agent is stuck, and the guest goes, the root's processes with it.
    if (!(await this.request({ type: "teardown", root }, SETUP_MS))) this.lose();
  }

  // Settles once all of the VM has gone.
  stop(): Promise<void> {
    this.lose();
    return this.vm.kill();
  }

  // Whether it has gone, though all of its VM may not have yet.
  get ended(): boolean {
    return this.left;
  }

  private lose(): void {
    if (this.left) return;
    this.left = true;
    clearInterval(this.keepalive);
    this.control.close();
    void this.vm.kill().catch(() => {}).then(this.leave);
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
    const failure = await Promise.race([guest.ready(operation.root, operation.folder, operation.ended), aborted(signal)]);
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

  private start(boot: BootVm): Promise<Guest> {
    const booting = Guest.boot(boot, this.options, this.halt.signal);
    booting.then((guest) => guest.gone, () => {}).finally(() => {
      if (this.guest === booting) this.guest = null;
    });
    return booting;
  }
}
