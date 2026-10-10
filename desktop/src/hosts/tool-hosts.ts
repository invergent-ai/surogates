// Runs each operation on its root session's folder: one tool host per root,
// started on that root's first operation (spec, Section 1). The folder comes from
// the app's own record of the binding, never from the request. What the root runs in
// the VM asks the chat's approvals about the hosts its connections reach past the
// package hosts.
//
// A project thread's root is bound to a copy of its folder (spec, Section 13): its host is
// started on the copy, which the copies have the guest make first, and is asked by the folder's
// path; its commands share the copy in the guest at that path. Nothing of a thread's works in
// the folder itself: without a copy it works nowhere. A landing of the thread's writes the
// folder through a host of its own on it, which holds the folder from the landing's first step
// until it is forgotten: only its helper's land kind writes there. What it kept goes only where the folder's history
// says the landing may go, and the forgetting names every step the helper holds a record of. A thread deleted while
// its landing has written the folder keeps that landing's host until it is forgotten.

import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { FolderGuards } from "../binding/folder.js";
import { spawnClean } from "../clean-child.js";
import { recordsOf } from "../files/land.js";
import { lostWith, type ProcessHandle } from "../guest/processes.js";
import type { Copy, Handle, Opened } from "../history/copies.js";
import { forgettingOf, NOT_A_FORGETTING_ASKED, unrecorded } from "../history/kinds.js";
import { keptOf } from "../history/place.js";
import type { Binding } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import { forgettable } from "../vm/history.js";
import type { Folder, ProcessesChange } from "../vm/manager.js";
import {
  FOLDER_UNAVAILABLE, type FromHost, type HostStart, type NetworkAnswer, type NetworkAsk, type ToHost,
} from "./messages.js";

// The same from src/hosts and from dist/hosts.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
const HOST = join(PACKAGE, "dist", "hosts", "host.js");
// The app's own plain node, beside its files (scripts/node.sh puts it there). The file hosts run
// on it, and their helpers in srt, so no process runs Electron as Node (spec, Section 11), and
// none runs a node of the user's.
export const NODE = join(PACKAGE, "bin", "node");
// Read-only folders a sandbox may read, and no chat's folder may hold: the running program's
// (Electron's, in the app) and the app's files, its node among them.
export const APP_DIRS = [dirname(process.execPath), PACKAGE];
const STOP_TIMEOUT_MS = 5_000;
export const START_TIMEOUT_MS = 30_000;
// How long a host with nothing to do keeps its folder: another chat may want it.
export const IDLE_MS = 120_000;
// How long a landing's host waits for the folder (spec, Section 13, "Waits"): a chat's host lets it go IDLE_MS after
// its last operation, and another landing's once that landing is forgotten, or IDLE_MS after its last step.
export const LAND_WAIT_MS = IDLE_MS + 30_000;
// How long the app's quit waits for a step a landing's helper runs (files/land.ts); every other stop of its host waits
// for the step whole. Past it the host is stopped: the step's record, written before each of its moves, names what it
// was doing, and it is put back as this computer's tools next start, or by the first step of the next landing's helper
// in that folder. A step stages a file of at most a GiB, about ten seconds on a disk that writes 100 MiB a second; one
// that copies a replaced file of several GiB between two filesystems can run past it, and its copy goes on from there.
export const QUIT_STEP_MS = 30_000;
// How long a deleted thread's landing's host that has written nothing in the folder is given for the step its helper
// runs, a look or a forgetting, before it is stopped: the deletion is answered at once, and its folder held no longer
// than this past it. Past it, what the step was doing is named by its record, and is put back as the quit's is. A
// landing that has written is never stopped for its thread's deletion.
export const RETIRE_STEP_MS = QUIT_STEP_MS;
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// A step a turn's landing asks for a landing it only settles, one another left running in the folder: named so by the
// server, under the turn's own name, and none of the turn's own landing.
const SETTLES = /^land:[^:]+:settle:/;

export const CANCELLED: Outcome = {
  error: { type: "cancelled", message: "The session stopped this before the computer finished it" },
};
export const HOST_STOPPED: Outcome = {
  error: {
    type: "interrupted",
    message: "interrupted: the computer's tool host stopped while this ran, so it was not run again. Check before repeating it.",
  },
};
export const NOT_BOUND: Outcome = { error: { type: "binding", message: "This folder was not confirmed on this computer" } };

const unavailable = (why: string): Outcome => ({
  error: { type: "unavailable", message: `This computer could not open the folder's sandbox: ${why}` },
});
// A root bound to a copy works in its copy or nowhere: with nothing to make copies, nowhere.
const NO_COPIES = unavailable("it has none to make this thread's copy of its folder in");
const QUITTING = unavailable("the app is quitting");
// How often a thread's copy is opened for one host, where it is told each time that the copy is not vouched for.
const OPENINGS = 4;
// What a thread's operation is answered that its host could not do, its copy made again while it was asked.
const madeAgain = (at: string): Outcome => unavailable(`the copy of ${at} this thread works in was made again while this was asked, so it was not done. Ask again`);

// A step of a landing that came to its host as the host was told to go: nothing of it was done.
const NOT_BEGUN = unavailable("the host of this landing was stopping, so this step of it was not begun. Ask again");
// What waited for a host that stopped, or was never made, before it was ready.
const STOPPED_STARTING = unavailable("its tool host stopped while it was starting");

/** Another held *folder* for as long as a landing waits for it: nothing of the landing began, and it can be asked again. */
export const folderBusy = (folder: string): Outcome => ({
  error: { type: "busy", message: `Another chat on this computer is working in ${folder}, so nothing of this landing was done. It can land once that one is done` },
});
/** A landing asked of a chat that is no project's thread: its operations wrote *folder* themselves, and it has no copy to land from. */
export const nothingToLand = (folder: string): Outcome => ({
  error: { type: "unsupported", message: `This chat works in ${folder} itself, so it has no copy of it to land from` },
});

// The hook guard around a process operation that runs in the VM: its refusal before it
// and its look after it, its refusal alone, or neither.
export type Guard = "around" | "before" | null;

// What a host needs of a root's binding: its folder, that folder's identity when it was bound,
// and the thread whose copy of it the root works in, if it is a project's thread.
export type BoundFolder = Pick<Binding, "folder" | "dev" | "ino" | "boot" | "history">;

// What a root's host holds, which its commands in the guest share: its chat's folder, or a thread's copy of one,
// shared at the path of the folder it is a copy of.
export interface Works {
  folder: Folder;
  at?: string;
}

// Who has a thread's copy made before its host starts on it, and lets it go once that host has gone
// (history/copies.ts): each copy it gives comes with that host's hold on it, closed once. And who asks the folder's
// history for the thread's copy: here, whether what a landing kept may be forgotten.
export interface ThreadCopies {
  open(root: string, bound: BoundFolder, signal: AbortSignal): Promise<Opened>;
  close(handle: Handle): void;
  ask(root: string, bound: BoundFolder, action: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome>;
}

export interface HostProcess {
  send(message: ToHost): void;
  onMessage(listener: (message: FromHost) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
}

export interface ForkOptions {
  script?: string; // the host's script; its default is dist/hosts/host.js
  execPath?: string; // the node it runs on; its default is the app's own
}

export function forkHost(options: ForkOptions = {}): HostProcess {
  // Spawned with an IPC channel, never forked: once Electron's RunAsNode fuse is off, its
  // child_process.fork() throws in every process of the app's, whatever node it is given.
  // Its own process group: srt's socat bridges are the host's children, outside
  // the sandbox, and do not die with it, so a host that goes takes its group
  // along. Its stdout goes to stderr: the app's stdout may carry other things.
  // Its environment is named, never the app's: a plain node acts on NODE_OPTIONS, NODE_PATH,
  // OPENSSL_CONF and the like, and srt on CLAUDE_CODE_TMPDIR, none of which the user's may set for it.
  // --disable-sigusr1: a plain node opens its inspector on SIGUSR1, which any process of the user's can
  // send, and has no fuse to refuse it as Electron has.
  // With its three standard descriptors and its channel, and nothing else the app has open.
  const child = spawnClean(options.execPath ?? NODE, ["--disable-sigusr1", options.script ?? HOST], {
    detached: true,
    stdio: ["ignore", 2, 2, "ipc"],
    env: { PATH: process.env.PATH ?? "", HOME: homedir() },
  });
  const listeners: Array<() => void> = [];
  let exited = false;
  const killGroup = () => {
    try {
      if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
    } catch {
      // The group has gone.
    }
  };
  // The host has gone, once: when it exits or closes, or when it never spawned
  // (an error and no pid, with no exit after). After that its group's id may be
  // somebody else's, so nothing signals it again.
  const gone = () => {
    if (exited) return;
    killGroup();
    exited = true;
    for (const listener of listeners) listener();
  };
  // A send to a host that has gone: its exit is what counts. One that never spawned says why, once:
  // a missing or broken bin/node answers every operation unavailable, and this line tells it.
  child.on("error", (error) => {
    if (child.pid !== undefined || exited) return;
    // console.error, never stderr itself: in Electron's main, a write to a stderr whose reader has gone
    // is an uncaught error that stalls the app; the console ignores it.
    console.error(`the file host could not start: ${error.message}`);
    gone();
  });
  // Started through the line that closes the app's descriptors, a node that is not there, or is no
  // program, is that line's 127, and never an error of the spawn.
  child.on("exit", (code) => {
    if (code === 127 && !exited) console.error(`the file host could not start: ${options.execPath ?? NODE} is not there, or cannot be run`);
    gone();
  });
  child.on("close", gone);
  return {
    send: (message) => {
      child.send(message);
    },
    onMessage: (listener) => {
      child.on("message", (message) => listener(message as FromHost));
    },
    onExit: (listener) => {
      if (exited) listener();
      else listeners.push(listener);
    },
    kill: () => {
      if (!exited) killGroup();
    },
  };
}

// Who decides what a root's commands may reach past the package hosts: the approvals.
export interface NetworkApprovals {
  // A destination one of the root's commands asked for. Settles once *signal* aborts (its host went).
  askNetwork(root: string, asked: NetworkAsk, signal: AbortSignal): Promise<NetworkAnswer>;
}

export interface ToolHostsOptions {
  bindingOf(rootSessionId: string): BoundFolder | undefined;
  // Without it, a root bound to a copy has nowhere to work, and every operation of it is refused.
  copies?: ThreadCopies;
  network?: NetworkApprovals; // without it, every destination off the package hosts is refused
  dataDir: string;
  // The app's own cache folder, <cache home>/surogate, by the cache home the app itself uses: no
  // chat's folder may hold it or lie in it.
  cacheDir: string;
  env: Record<string, string>;
  appDirs?: string[];
  bwrapPath?: string;
  spawnHost?: () => HostProcess;
  startTimeoutMs?: number;
  idleMs?: number;
  // LAND_WAIT_MS unless a test says.
  landWaitMs?: number;
  // Waited for before a root's host lets its folder go, so what else holds the folder
  // lets it go first; told too when a host went by itself.
  release?(root: string): Promise<void>;
  // Told each time a root's processes in the VM change, or a host goes: what liveRoots() names may have changed.
  changed?(): void;
}

export class ToolHosts implements Executor {
  private readonly hosts = new Map<string, Host>();
  // Each thread's landing's host, on the folder itself, by its root: from the landing's first step until it is
  // forgotten, or has had no step for idleMs.
  private readonly landers = new Map<string, Host>();
  // Every host until it exits: one stopping because it had nothing to do is no longer in hosts.
  private readonly live = new Set<Host>();
  // A thread's host while its copy is opened for it: the root's operations that come meanwhile wait for that one.
  private readonly starting = new Map<string, Starting>();
  // A landing's host while the thread's copy is opened for it: the landing's steps that come meanwhile wait for that one.
  private readonly landing = new Map<string, Starting>();
  // The root of each host on a thread's copy, until it exits.
  private readonly onCopies = new Map<Host, string>();
  // A deleted thread's binding, by its root, while the landing's host it kept is there: the landing's steps still reach it.
  private readonly deleted = new Map<string, BoundFolder>();
  // How often each thread's root was told its copy is not vouched for: told while its copy was being opened, the copy
  // that open gives may be the one meant.
  private readonly told = new Map<string, number>();
  // Aborted at the app's quit: a copy's opening for a host that will not start is waited for no longer.
  private readonly halt = new AbortController();
  private stopping: Promise<void> | undefined;

  constructor(private readonly options: ToolHostsOptions) {}

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (operation.kind === "bind") return NOT_BOUND;
    const host = await this.hostOf(operation.sessionId, signal);
    return host instanceof Host ? host.run(operation, signal) : host;
  }

  /**
   * *inner*, a process operation that runs in the VM, while the root's host holds what the root
   * works in: its lock, and the hook guard as *guard* says (Host.guarded). *inner* is given what
   * the guest shares for the root: its folder, or its copy at the folder's path.
   */
  async guarded(
    operation: Operation, signal: AbortSignal, guard: Guard,
    inner: (works: Works, signal: AbortSignal, ended: ProcessHandle[]) => Promise<Outcome>,
  ): Promise<Outcome> {
    const host = await this.hostOf(operation.sessionId, signal);
    if (!(host instanceof Host)) return host;
    return host.guarded(operation, signal, guard, (aborted, ended) => inner(host.works, aborted, ended));
  }

  /**
   * One step of a thread's landing, the file helper's land kind (files/land.ts), in the landing's own host on the
   * folder itself: started at the landing's first step, given the thread's copy to read and the folder's kept folder to
   * keep what it replaces in, and stopped once the landing is forgotten, or has had no step for idleMs. Until then it
   * holds the folder, so one landing at a time writes it and no chat's host starts on it; the thread's host on its copy
   * holds the copy, and goes on beside it. A chat that is no thread has nothing to land.
   */
  async land(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (operation.kind !== "land") return { error: { type: "unsupported", message: `A landing's host cannot do '${operation.kind}'` } };
    if (this.stopping) return QUITTING;
    const root = operation.sessionId;
    const bound = SESSION_ID.test(root) ? this.options.bindingOf(root) : undefined;
    const binding = bound ?? this.deleted.get(root);
    if (!binding) return FOLDER_UNAVAILABLE;
    if (binding.history === undefined) return nothingToLand(binding.folder);
    const { copies } = this.options;
    if (!copies) return NO_COPIES;
    const { action, saga } = operation.args;
    // A hold given back, by a name no landing has, where the thread neither holds the folder nor is taking it: there is
    // nothing to let go, and no host is started to say so, which would wait for the folder first.
    const holds = this.landers.has(root) || this.landing.has(root);
    if (action === "forget" && typeof saga === "string" && saga.startsWith("hold:") && !holds) return { ok: {} };
    const forgetting = action === "forget" ? forgettingOf(operation.args) : null;
    if (action === "forget" && !forgetting) return NOT_A_FORGETTING_ASKED;
    // A deleted thread's landing goes on in the host it kept, and in no other.
    const known = this.landers.get(root);
    if (!bound && !known) return FOLDER_UNAVAILABLE;
    const host = known ?? await this.once(this.landing, root, signal, (wanted) => this.onLanding(root, binding, copies, wanted));
    if (!(host instanceof Host)) return host;
    if (!forgetting) return host.run(operation, signal);
    // What a landing kept goes only by the folder's history's word, the landing recorded or each file it applied as it was
    // before; and only where the forgetting names every step the helper holds a record of. Its helper's records are the
    // proof of what was applied; the steps named, the server's word. Asked with no step of the landing's running, from
    // the history's answer to the forgetting.
    const gate = async (aborted: AbortSignal) => {
      const leave = forgettable(await copies.ask(root, binding, "forget", { saga: forgetting.saga, applied: forgetting.applied }, aborted));
      return leave ?? unrecorded(recordsOf(host.kept!, forgetting.saga), forgetting.applied);
    };
    // Forgotten, the landing is over: the folder is let go at once, for the next landing or a chat. Not at the forgetting
    // of a landing the turn only settled, one another left running in the folder: the turn holds the folder from its
    // settle to its own landing, so no other landing begins between the two.
    return host.alone(operation, signal, gate, (outcome) => "ok" in outcome && !SETTLES.test(operation.invocationId));
  }

  /**
   * The copy *root*'s hosts work in is its copy no more (history/copies.ts): the guest made it again,
   * left it other than whole, or its place is being let go. Each of them goes, out of the list first, so
   * the root's next operation starts a host on the copy the guest opens then. Its landing's host is none of
   * them: it lands only the bytes its turn committed, holds the folder until that landing is over, and goes on.
   */
  replaced(root: string): void {
    this.told.set(root, (this.told.get(root) ?? 0) + 1);
    for (const [host, of] of this.onCopies) if (of === root) host.finish();
  }

  /**
   * *root*'s hosts go now rather than once idle, each letting go of what it holds, and so do those its operations are
   * starting: what they hold is no longer what the root works in, as when its chat is deleted. Its landing's host
   * begins no step after, and goes once its helper has ended the ones it runs. Settles once every one of them has
   * gone. Never rejects.
   */
  dismiss(root: string): Promise<void> {
    return this.going(root);
  }

  /**
   * *root*'s chat was deleted, and its binding *bound* is forgotten next (binding/binder.ts). Its hosts on its copy go,
   * and so does its landing's, given RETIRE_STEP_MS for the step its helper runs; its copy stays, with what it did not
   * land. Answered at once: nothing waits for them. But a landing's host that has written in the folder, an apply or a
   * put-back sent, is kept, with *bound*, so the steps that put that landing back or forget it still reach it, until it
   * is forgotten or has had no step for idleMs, as any landing's host.
   */
  retired(root: string, bound: BoundFolder): void {
    const landing = this.landers.get(root);
    const kept = landing?.wrote ? landing : undefined;
    if (kept) {
      this.deleted.set(root, bound);
      void kept.exited.then(() => {
        if (this.deleted.get(root) === bound) this.deleted.delete(root);
      });
    }
    void this.going(root, kept, RETIRE_STEP_MS);
  }

  /** The binding of a deleted thread whose landing's host was kept, while it is there: that landing's steps take it. */
  deletedLanding(root: string): BoundFolder | undefined {
    return this.deleted.get(root);
  }

  // *root*'s hosts and those its operations are starting go, but *kept*; a landing's given *stepMs* for its step.
  private async going(root: string, kept?: Host, stepMs?: number): Promise<void> {
    const going = new Set<Host>();
    const go = (host: Host | Outcome | undefined) => {
      if (!(host instanceof Host) || host === kept || going.has(host)) return;
      host.finish(stepMs);
      going.add(host);
    };
    for (const host of [this.hosts.get(root), this.landers.get(root), ...[...this.onCopies].filter(([, of]) => of === root).map(([host]) => host)]) go(host);
    for (const made of await Promise.all([this.starting.get(root)?.host, this.landing.get(root)?.host])) go(made);
    await Promise.all([...going].map((host) => host.exited));
  }

  /** Whether a root bound to a thread's copy has one made to work in here. */
  keepsCopies(): boolean {
    return this.options.copies !== undefined;
  }

  /**
   * A destination one of *root*'s commands in the VM asked for: the approvals' to decide
   * while something of the root runs, and denied when the root has no host, or nothing of it runs.
   */
  ask(root: string, asked: NetworkAsk): Promise<NetworkAnswer> {
    return this.hosts.get(root)?.ask(asked) ?? Promise.resolve("deny");
  }

  /** A root's background processes in the VM changed: its host keeps their handles, and stays while any lives. */
  processes(root: string, change: ProcessesChange): void {
    this.hosts.get(root)?.processes(change);
    this.options.changed?.();
  }

  /** The roots with a background process alive in the VM, whose work a quit would end. */
  liveRoots(): string[] {
    return [...this.hosts].filter(([, host]) => host.lives).map(([root]) => root);
  }

  // The guards each host checks its folder against: the binder's must be these, or the
  // sheet could accept a folder every host refuses. Without a HOME, as a host would, it throws.
  guards(): FolderGuards {
    const home = this.options.env.HOME;
    if (!home) throw new Error("the app's environment has no HOME");
    return { home, dataDir: this.options.dataDir, cacheDir: this.options.cacheDir, appDirs: this.options.appDirs ?? APP_DIRS };
  }

  // The app's quit: each folder's other holders get as long to let it go as its host
  // gets to stop. What holds it next, the VM, is stopped after, whether or not they did.
  // A landing's step its helper runs gets QUIT_STEP_MS to end.
  stop(): Promise<void> {
    this.halt.abort();
    this.stopping ??= this.stopHosts(STOP_TIMEOUT_MS, QUIT_STEP_MS);
    return this.stopping;
  }

  // The device's access ended: every host stops, and its background processes with it.
  // The next operation for a root starts a new host, which answers for them from the record.
  // A landing's host first ends the steps its helper runs.
  end(): Promise<void> {
    return this.stopHosts();
  }

  private async stopHosts(letGoMs?: number, stepMs?: number): Promise<void> {
    const hosts = [...this.live];
    this.hosts.clear();
    this.landers.clear();
    // A host still being started is started no more: its copy's hold is given back as it is opened, and an operation
    // that comes after has a start of its own.
    for (const starts of [this.starting, this.landing]) {
      for (const start of starts.values()) start.stopped = true;
      starts.clear();
    }
    await Promise.all(hosts.map(async (host) => {
      let timer: NodeJS.Timeout | undefined;
      await Promise.race([host.letGo(), new Promise((resolve) => {
        if (letGoMs !== undefined) timer = setTimeout(resolve, letGoMs);
      })]);
      clearTimeout(timer);
      await host.stop(stepMs);
    }));
  }

  // The host of *root*, started if it has none: on its chat's folder, or, for a thread, on its copy,
  // opened first. Or why it has none: a thread never works in its folder for want of its copy.
  private async hostOf(root: string, signal: AbortSignal): Promise<Host | Outcome> {
    if (this.stopping) return QUITTING;
    const binding = SESSION_ID.test(root) ? this.options.bindingOf(root) : undefined;
    if (!binding) return FOLDER_UNAVAILABLE;
    const known = this.hosts.get(root);
    if (known) return known;
    if (binding.history === undefined) return this.hostFor(root, { folder: { path: binding.folder, dev: binding.dev, ino: binding.ino, boot: binding.boot } });
    const { copies } = this.options;
    if (!copies) return NO_COPIES;
    return this.once(this.starting, root, signal, (wanted) => this.onCopy(root, binding, copies, wanted));
  }

  // The host *begin* makes for *root*, made once for every caller that comes while its copy is opened, through *starts*.
  // A cancel is answered at once, and the copy's opening goes on: a host is made of it only while a caller still
  // waits for one (*begin*'s wanted), and the hosts were not stopped meanwhile. A host no step reaches is never made.
  private once(starts: Map<string, Starting>, root: string, signal: AbortSignal, begin: (wanted: () => boolean) => Promise<Host | Outcome>): Promise<Host | Outcome> {
    if (signal.aborted) return Promise.resolve(CANCELLED);
    let start = starts.get(root);
    if (!start) {
      // Wanted is asked once the copy is opened, never before: by then this is the start's record.
      const begun: Starting = {
        host: begin(() => begun.waiting > 0 && !begun.stopped).catch((error: unknown) => unavailable(error instanceof Error ? error.message : String(error))),
        waiting: 0,
        stopped: false,
      };
      starts.set(root, begun);
      void begun.host.then(() => {
        if (starts.get(root) === begun) starts.delete(root);
      });
      start = begun;
    }
    const waited = start;
    waited.waiting += 1;
    return new Promise((resolve) => {
      const cancel = () => {
        waited.waiting -= 1;
        resolve(CANCELLED);
      };
      signal.addEventListener("abort", cancel, { once: true });
      void waited.host.then(resolve).finally(() => signal.removeEventListener("abort", cancel));
    });
  }

  // *root*'s host on its copy, once the copies have it opened, holding the copy from then until it has gone. A copy
  // the root was told is not vouched for while it was opened is let go, and opened again.
  private async onCopy(root: string, binding: BoundFolder, copies: ThreadCopies, wanted: () => boolean): Promise<Host | Outcome> {
    for (let turn = 0; turn < OPENINGS; turn += 1) {
      const told = this.told.get(root) ?? 0;
      const opened = await copies.open(root, binding, this.halt.signal);
      if (this.stopping) {
        if ("handle" in opened) copies.close(opened.handle);
        return QUITTING;
      }
      if ("failed" in opened) return opened.failed;
      const { copy, handle } = opened;
      // Nothing waits for a host any more, or the hosts were stopped meanwhile: none is made to hold the copy.
      if (!wanted()) {
        copies.close(handle);
        return STOPPED_STARTING;
      }
      if ((this.told.get(root) ?? 0) !== told) {
        copies.close(handle);
        continue;
      }
      let host: Host;
      try {
        // What the guest shared of the root's for a host on the copy before this one is let go first.
        const before = Promise.all([...this.onCopies].filter(([, of]) => of === root).map(([earlier]) => earlier.letGo())).then(() => {});
        host = this.hostFor(root, { folder: copy.folder, at: copy.at }, before);
      } catch (error) {
        // No host holds it: its hold goes now.
        copies.close(handle);
        throw error;
      }
      this.onCopies.set(host, root);
      // Let go once the host has stopped, and what its root ran in the guest has ended with what the guest shared for it.
      void host.exited.then(() => host.letGo()).then(() => {
        this.onCopies.delete(host);
        copies.close(handle);
      });
      return host;
    }
    return unavailable(`the copy of ${binding.folder} this thread works in was made again each time it was opened`);
  }

  // *root*'s landing's host, once the copies have its thread's copy opened for it: it holds that copy by a hold of its
  // own, let go once it has stopped. It is none of the hosts on the copy, whose commands share it in the guest.
  private async onLanding(root: string, binding: BoundFolder, copies: ThreadCopies, wanted: () => boolean): Promise<Host | Outcome> {
    const opened = await copies.open(root, binding, this.halt.signal);
    if (this.stopping) {
      if ("handle" in opened) copies.close(opened.handle);
      return QUITTING;
    }
    if ("failed" in opened) return opened.failed;
    const { copy, handle } = opened;
    // No host holds it where none is made: its hold goes now. None is made where no step waits for it any more, or the
    // hosts were stopped meanwhile, to hold the folder.
    if (!wanted()) {
      copies.close(handle);
      return STOPPED_STARTING;
    }
    let kept: string;
    try {
      kept = keptOf(this.options.dataDir, copy.place);
    } catch {
      copies.close(handle);
      return unavailable(`the app's data, where a landing in ${binding.folder} keeps the files it replaces, is not there or is not the app's own`);
    }
    let host: Host;
    try {
      host = this.lander(root, binding, copy, kept);
    } catch (error) {
      copies.close(handle);
      throw error;
    }
    void host.exited.then(() => copies.close(handle));
    return host;
  }

  // A landing's host for *root*: on the folder *binding* holds, from *copy*, keeping what it replaces in *kept*, where
  // the app keeps what that folder's landings replace. It runs no command: nothing of it asks for the network, and
  // nothing of it is in the guest to let go.
  private lander(root: string, binding: BoundFolder, copy: Copy, kept: string): Host {
    const works: Works = { folder: { path: binding.folder, dev: binding.dev, ino: binding.ino, boot: binding.boot } };
    const lockWaitMs = this.options.landWaitMs ?? LAND_WAIT_MS;
    // A working folder of its own: the thread's host on its copy runs beside it, and each sandbox writes its own.
    const start: HostStart = { ...this.startOf(works, join(this.options.dataDir, "tmp", `${root}.land`)), landing: { copy: copy.folder.path, kept }, lockWaitMs };
    // Its wait for the folder, then a host's own start. What a landing there cut short its helper puts back in the first
    // step it is asked, which no stop of this host cuts (files/land.ts).
    const startMs = lockWaitMs + (this.options.startTimeoutMs ?? START_TIMEOUT_MS);
    const host = new Host(
      (this.options.spawnHost ?? forkHost)(), start, works, startMs, this.options.idleMs ?? IDLE_MS, () => {
        if (this.landers.get(root) === host) this.landers.delete(root);
      },
      () => Promise.resolve("deny"),
      () => Promise.resolve(),
    );
    this.landers.set(root, host);
    this.keep(host);
    return host;
  }

  private hostFor(root: string, works: Works, before?: Promise<void>): Host {
    const { network } = this.options;
    const ask = (asked: NetworkAsk, signal: AbortSignal): Promise<NetworkAnswer> =>
      network ? network.askNetwork(root, asked, signal) : Promise.resolve("deny");
    const host = new Host(
      (this.options.spawnHost ?? forkHost)(), this.startOf(works, join(this.options.dataDir, "tmp", root)), works, this.options.startTimeoutMs ?? START_TIMEOUT_MS,
      this.options.idleMs ?? IDLE_MS, () => {
        if (this.hosts.get(root) === host) this.hosts.delete(root);
      },
      ask,
      () => this.options.release?.(root) ?? Promise.resolve(),
      before,
    );
    this.hosts.set(root, host);
    this.keep(host);
    return host;
  }

  // A host's start on what *works* holds, with the helper's working folder *tmp*.
  private startOf(works: Works, tmp: string): HostStart {
    const { dataDir, cacheDir, env, bwrapPath } = this.options;
    const { path, dev, ino, boot } = works.folder;
    return {
      type: "start",
      folder: path,
      expect: { dev, ino, boot: boot ?? "" },
      ...(works.at === undefined ? {} : { at: works.at }),
      tmp,
      dataDir,
      cacheDir,
      env,
      appDirs: this.options.appDirs ?? APP_DIRS,
      ...(bwrapPath ? { bwrapPath } : {}),
    };
  }

  // *host* is counted as live until it exits.
  private keep(host: Host): void {
    this.live.add(host);
    void host.exited.then(() => {
      this.live.delete(host);
      this.options.changed?.();
    });
  }
}

// Whether *signal* aborts before *work* settles; *work* goes on either way.
function aborts(work: Promise<unknown>, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(true);
  return new Promise((resolve) => {
    const abort = () => resolve(true);
    signal.addEventListener("abort", abort, { once: true });
    void work.then(() => resolve(false)).finally(() => signal.removeEventListener("abort", abort));
  });
}

// A host being made for a root while its copy is opened: the host, or why there is none; how many of the root's
// operations still wait for it; and whether the hosts were stopped meanwhile.
interface Starting {
  host: Promise<Host | Outcome>;
  waiting: number;
  stopped: boolean;
}

class Host {
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private readonly started: Promise<Outcome | null>;
  readonly exited: Promise<void>;
  private readonly startTimer: NodeJS.Timeout;
  private settleStart: (failure: Outcome | null) => void = () => {};
  private gone = false;
  private running = 0;
  private live = 0;
  private idleTimer: NodeJS.Timeout | undefined;
  // Its open network prompts: dismissed when it goes, or once nothing of its root runs.
  private prompts = new AbortController();
  private letting: Promise<void> | null = null;
  // The handles of its root's background processes in the VM, which every
  // operation there carries: from the folder's record at its start, then as they change.
  private handles: ProcessHandle[] = [];
  // Once its file host said ready: what came of its root's processes before is not its own.
  private readied = false;
  // Told to go before it was idle, as a host on a copy that is its thread's no more.
  private finished = false;
  // Told to stop: what it was sent ends, and nothing of a landing's is sent after.
  private closing = false;
  // A landing's host: its helper writes the user's folder itself, and no stop of it cuts a step it was sent. Where it
  // keeps the files the folder's landings replace.
  private readonly lands: boolean;
  readonly kept: string | undefined;
  // A landing's host's steps its helper was sent and has not answered, by their ids: a cancelled one's too, which the
  // helper runs all the same. And who waits until there are none.
  private readonly unanswered = new Map<string, number>();
  private quiet: Array<() => void> = [];
  // A landing's host was sent a step that writes the folder: an apply, or a put-back.
  private written = false;
  // Settled once the last step sent alone (alone) is answered: a landing's next step is sent only after.
  private lone: Promise<void> = Promise.resolve();

  constructor(
    private readonly process: HostProcess,
    start: HostStart,
    // What it holds, which its root's commands in the guest share.
    readonly works: Works,
    startTimeoutMs: number,
    private readonly idleMs: number,
    private readonly onGone: () => void,
    private readonly askUser: (asked: NetworkAsk, signal: AbortSignal) => Promise<NetworkAnswer>,
    private readonly release: () => Promise<void>,
    // Settled once the guest has let go of what it shared of the root's for a host before this one: its root's
    // commands run only after, or one could run in what that host held.
    private readonly before: Promise<void> = Promise.resolve(),
  ) {
    this.lands = start.landing !== undefined;
    this.kept = start.landing?.kept;
    this.started = new Promise((resolve) => {
      this.settleStart = (failure) => {
        clearTimeout(this.startTimer);
        resolve(failure);
      };
    });
    // A host that never says ready or failed (a hang in srt, or in a stat on a
    // stuck mount) would hold every operation for its root: it is killed, and
    // the exit answers the rest.
    this.startTimer = setTimeout(() => {
      this.onGone();
      this.settleStart(unavailable(`its tool host did not start within ${startTimeoutMs / 1000} seconds`));
      this.process.kill();
    }, startTimeoutMs);
    this.exited = new Promise((resolve) => {
      process.onExit(() => {
        this.leave();
        resolve();
      });
    });
    process.onMessage((message) => this.received(message));
    this.send(start);
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    return this.busy(async () => {
      const failure = await this.ready(signal);
      if (failure) return failure;
      // A landing's step is sent after one sent alone has been answered.
      if (this.lands && await aborts(this.lone, signal)) return CANCELLED;
      // A landing's host told to go begins no step: the folder is the user's, and what it was sent ends first.
      if (this.lands && (this.finished || this.closing)) return NOT_BEGUN;
      const { action } = operation.args;
      if (this.lands && (action === "apply" || action === "unapply")) this.written = true;
      return this.request({ type: "op", id: operation.id, kind: operation.kind, args: operation.args }, signal);
    });
  }

  /**
   * A landing's *operation*, sent alone: once every step sent before it has been answered, and only where *gate*,
   * asked then, has nothing against it; no other step of the landing's is sent from then until it is answered. So
   * what *gate* finds of the folder and of what the landing kept is what the step finds. Where *ends* says its answer
   * ends the landing, the host is told to go before any step after it is let through. Its answer, *gate*'s refusal,
   * or why it was not sent.
   */
  alone(
    operation: Operation, signal: AbortSignal, gate: (signal: AbortSignal) => Promise<Outcome | null>, ends: (outcome: Outcome) => boolean,
  ): Promise<Outcome> {
    return this.busy(async () => {
      const failure = await this.ready(signal);
      if (failure) return failure;
      const before = this.lone;
      let over = () => {};
      this.lone = new Promise((resolve) => {
        over = resolve;
      });
      try {
        if (await aborts(before, signal)) return CANCELLED;
        while (this.unanswered.size > 0 && !this.gone) {
          if (await aborts(this.stepsEnded(), signal)) return CANCELLED;
        }
        if (this.finished || this.closing) return NOT_BEGUN;
        const refused = await gate(signal);
        if (refused) return refused;
        if (this.finished || this.closing) return NOT_BEGUN;
        const outcome = await this.request({ type: "op", id: operation.id, kind: operation.kind, args: operation.args }, signal);
        if (ends(outcome)) this.finish();
        return outcome;
      } finally {
        over();
      }
    });
  }

  // Whether a landing's host was sent a step that writes the folder.
  get wrote(): boolean {
    return this.written;
  }

  // Its work is over before it has been idle: out of the list first, so the next operation starts a new host, then
  // its folder's other holders let it go, and it stops, a landing's once its steps have ended, within *stepMs* if given. Once.
  finish(stepMs?: number): void {
    if (this.finished) return;
    this.finished = true;
    clearTimeout(this.idleTimer);
    this.onGone();
    void this.letGo().then(() => this.stop(stepMs));
  }

  /**
   * *inner* while this host holds the folder, so the folder is not let go mid-way.
   * With *guard*: the hook guard's refusal first; "around" then, whatever the outcome,
   * a cancel's too, its look after, which no cancel stops; the outcome carries its notice.
   */
  guarded(
    operation: Operation, signal: AbortSignal, guard: Guard, inner: (signal: AbortSignal, ended: ProcessHandle[]) => Promise<Outcome>,
  ): Promise<Outcome> {
    return this.busy(async () => {
      const failure = await this.ready(signal);
      if (failure) return failure;
      if (guard) {
        const refused = await this.request({ type: "refusal", id: operation.id, run: guard === "around" }, signal);
        if (!("ok" in refused)) return refused;
      }
      // Nothing to wait for but on a thread's copy that another host held before.
      await this.before;
      // Told to go, it is letting go of what the guest shares for it: a command sent now would have the guest share its
      // copy again after that, and nothing would let that share go.
      if (this.finished && this.works.at !== undefined) return madeAgain(this.works.at);
      const outcome = await inner(signal, this.handles);
      return guard === "around" ? this.request({ type: "after", id: operation.id, outcome }) : outcome;
    });
  }

  // Its root's processes in the VM changed: the host's record keeps their handles, and a
  // host with any alive is never idle. Gone: those still running ended with their sandbox.
  // Until its file host is ready, a change is an earlier host's or an earlier guest's: its
  // operations reach the guest only after, and the record's handles, which ready brings, have all ended.
  processes(change: ProcessesChange): void {
    if (!this.readied) return;
    this.handles = "gone" in change ? lostWith(this.handles) : change.handles;
    this.live = "gone" in change ? 0 : change.live;
    this.send({ type: "handles", handles: this.handles, live: this.live });
    this.idle();
  }

  // Whether a background process of its root lives in the VM.
  get lives(): boolean {
    return this.live > 0 && !this.gone;
  }

  // Counted while it runs: a host with work is never idle.
  private async busy(work: () => Promise<Outcome>): Promise<Outcome> {
    this.running += 1;
    clearTimeout(this.idleTimer);
    try {
      return this.answered(await work());
    } finally {
      this.running -= 1;
      this.idle();
    }
  }

  // *outcome*, as the root is answered it. A host on a thread's copy, or the guest's share of it, that finds
  // another folder at the copy's path has its copy made again under it: its sandbox holds the folder that was
  // there, so it goes, and the next operation has the copy opened again. Said by the folder, not as a folder gone.
  private answered(outcome: Outcome): Outcome {
    const { at } = this.works;
    if (at === undefined || !("error" in outcome) || outcome.error.type !== "folder_unavailable") return outcome;
    this.finish();
    return madeAgain(at);
  }

  // With no operation running and no background process alive, the host keeps its
  // folder for idleMs. Out of the list first: the next operation for this root starts a new host.
  // Never while it starts: its start is bounded by its own time.
  private idle(): void {
    clearTimeout(this.idleTimer);
    if (!this.readied || this.running > 0 || this.live > 0 || this.gone || this.finished || this.unanswered.size > 0) return;
    // No command or process of the root is left, so no connection waits on its prompts.
    this.prompts.abort();
    this.prompts = new AbortController();
    this.idleTimer = setTimeout(() => {
      this.onGone();
      void this.letGo().then(() => this.stop());
    }, this.idleMs).unref();
  }

  // Its folder's other holders let it go, once a host's life: before it stops, or after it went.
  letGo(): Promise<void> {
    this.letting ??= this.release().catch(() => {});
    return this.letting;
  }

  // Null once the host has started; otherwise why nothing runs. A cancel while it starts is answered at once.
  private async ready(signal: AbortSignal): Promise<Outcome | null> {
    let onAbort = () => {};
    const aborted = new Promise<Outcome>((resolve) => {
      onAbort = () => resolve(CANCELLED);
      signal.addEventListener("abort", onAbort, { once: true });
    });
    if (signal.aborted) onAbort();
    const failure = await Promise.race([this.started, aborted]);
    signal.removeEventListener("abort", onAbort);
    if (failure) return failure;
    if (this.gone) return unavailable("its tool host stopped; try again");
    if (signal.aborted) return CANCELLED;
    return null;
  }

  // The host's answer to *message*, by its id, or CANCELLED at once when *signal* aborts first, and the host is told.
  private request(message: Extract<ToHost, { type: "op" | "refusal" | "after" }>, signal?: AbortSignal): Promise<Outcome> {
    // One that went since: nothing would answer.
    if (this.gone) return Promise.resolve(HOST_STOPPED);
    return new Promise((resolve) => {
      const answer = (outcome: Outcome) => {
        signal?.removeEventListener("abort", cancel);
        resolve(outcome);
      };
      // A newer request may have this id by now; only this one's entry goes.
      const cancel = () => {
        if (this.pending.get(message.id) !== answer) return;
        this.pending.delete(message.id);
        this.send({ type: "cancel", id: message.id });
        resolve(CANCELLED);
      };
      this.pending.set(message.id, answer);
      signal?.addEventListener("abort", cancel, { once: true });
      if (this.lands) this.unanswered.set(message.id, (this.unanswered.get(message.id) ?? 0) + 1);
      this.send(message);
    });
  }

  // A landing's step its helper answered: once none is left, a stop that waits for them goes on, and the host may idle.
  private answeredStep(id: string): void {
    const left = (this.unanswered.get(id) ?? 0) - 1;
    if (left > 0) this.unanswered.set(id, left);
    else this.unanswered.delete(id);
    if (this.unanswered.size > 0) return;
    for (const done of this.quiet.splice(0)) done();
    this.idle();
  }

  // Once its helper has answered every step it was sent, or it has gone; or once *ms* have passed.
  private stepsEnded(ms?: number): Promise<void> {
    if (this.unanswered.size === 0 || this.gone) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = ms === undefined ? undefined : setTimeout(resolve, ms);
      this.quiet.push(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  /**
   * A landing's host first lets its helper end every step it was sent, each of which writes the user's folder, and
   * sends it none after; at the app's quit, for *stepMs* at most. Then it stops.
   */
  async stop(stepMs?: number): Promise<void> {
    clearTimeout(this.idleTimer);
    this.closing = true;
    if (this.lands) await this.stepsEnded(stepMs);
    this.send({ type: "stop" });
    const timer = setTimeout(() => this.process.kill(), STOP_TIMEOUT_MS);
    await this.exited;
    clearTimeout(timer);
  }

  // Never throws: abort listeners call it.
  private send(message: ToHost): void {
    try {
      this.process.send(message);
    } catch {
      // The host has gone; its exit answers what was pending.
    }
  }

  private received(message: FromHost): void {
    if (message.type === "ready") {
      this.handles = message.processes;
      this.readied = true;
      this.settleStart(null);
      // Ready, with nothing waiting for it, as when what it was started for was cancelled meanwhile: it idles.
      this.idle();
    } else if (message.type === "failed") {
      // A host that failed to start is exiting: the next operation starts a new one. One on a thread's copy says
      // in its own words, which name the folder, that the copy is not the one opened: its folder is not gone. A
      // landing's that waited for its folder in vain began nothing of the landing.
      this.onGone();
      if (message.folder && this.works.at === undefined) this.settleStart(FOLDER_UNAVAILABLE);
      else this.settleStart(message.busy && this.lands ? folderBusy(this.works.folder.path) : unavailable(message.message));
    } else {
      const answer = this.pending.get(message.id);
      this.pending.delete(message.id);
      answer?.(message.outcome);
      if (this.unanswered.has(message.id)) this.answeredStep(message.id);
    }
  }

  // A destination one of its root's commands asked for: every connection to it waits for
  // this answer. Fails closed: a choice that fails denies, and so does an ask that comes once
  // nothing of the root runs (a lookup that outlasted its command), with no prompt.
  ask(asked: NetworkAsk): Promise<NetworkAnswer> {
    if (this.running === 0 && this.live === 0) return Promise.resolve("deny");
    // The prompts' signal as it was checked: an end of the root's last process that comes before the prompt dismisses it.
    const { signal } = this.prompts;
    return Promise.resolve().then(() => this.askUser(asked, signal)).catch(() => "deny" as const);
  }

  private leave(): void {
    clearTimeout(this.idleTimer);
    this.prompts.abort();
    this.gone = true;
    this.onGone();
    void this.letGo();
    this.settleStart(STOPPED_STARTING);
    for (const answer of this.pending.values()) answer(HOST_STOPPED);
    this.pending.clear();
    // Nothing it was sent is run any more.
    this.unanswered.clear();
    for (const done of this.quiet.splice(0)) done();
  }
}
