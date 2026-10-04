// Runs each operation on its root session's folder: one tool host per root,
// started on that root's first operation (spec, Section 1). The folder comes from
// the app's own record of the binding, never from the request. A host's commands
// reach only the package hosts and what the chat's user allows: the approvals decide.

import { fork } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { FolderGuards } from "../binding/folder.js";
import type { Binding } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import {
  FOLDER_UNAVAILABLE, type FromHost, type HostStart, type NetworkAnswer, type NetworkAsk, type ToHost,
} from "./messages.js";

// The same from src/hosts and from dist/hosts.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
const HOST = join(PACKAGE, "dist", "hosts", "host.js");
// Read-only folders a sandbox needs, and no chat's folder may hold: the runtime and the app's files.
export const APP_DIRS = [dirname(process.execPath), PACKAGE];
const STOP_TIMEOUT_MS = 5_000;
export const START_TIMEOUT_MS = 30_000;
// How long a host with nothing to do keeps its folder: another chat may want it.
export const IDLE_MS = 120_000;
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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

// What a host needs of a root's binding: its folder, and that folder's identity when it was bound.
type BoundFolder = Pick<Binding, "folder" | "dev" | "ino" | "boot">;

export interface HostProcess {
  send(message: ToHost): void;
  onMessage(listener: (message: FromHost) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
}

export interface ForkOptions {
  script?: string; // the host's script; its default is dist/hosts/host.js
  execPath?: string; // the runtime to run it with
}

export function forkHost(options: ForkOptions = {}): HostProcess {
  // Its own process group: srt's socat bridges are the host's children, outside
  // the sandbox, and do not die with it, so a host that goes takes its group
  // along. Its stdout goes to stderr: the app's stdout may carry other things.
  const child = fork(options.script ?? HOST, [], {
    detached: true,
    stdio: ["ignore", 2, 2, "ipc"],
    ...(options.execPath ? { execPath: options.execPath } : {}),
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
  // A send to a host that has gone: its exit is what counts.
  child.on("error", () => {
    if (child.pid === undefined) gone();
  });
  child.on("exit", gone);
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
  // The hosts the chat's user allowed for the chat: each new tool host for the root starts with these.
  granted(root: string): readonly string[];
  // A destination one of the root's commands asked for. Settles once *signal* aborts (its host went).
  askNetwork(root: string, asked: NetworkAsk, signal: AbortSignal): Promise<NetworkAnswer>;
}

export interface ToolHostsOptions {
  bindingOf(rootSessionId: string): BoundFolder | undefined;
  network?: NetworkApprovals; // without it, every destination off the package hosts is refused
  dataDir: string;
  env: Record<string, string>;
  appDirs?: string[];
  bwrapPath?: string;
  spawnHost?: () => HostProcess;
  startTimeoutMs?: number;
  idleMs?: number;
}

export class ToolHosts implements Executor {
  private readonly hosts = new Map<string, Host>();
  // Every host until it exits: one stopping because it had nothing to do is no longer in hosts.
  private readonly live = new Set<Host>();
  private stopping: Promise<void> | undefined;

  constructor(private readonly options: ToolHostsOptions) {}

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (operation.kind === "bind") return NOT_BOUND;
    if (this.stopping) return unavailable("the app is quitting");
    const binding = SESSION_ID.test(operation.sessionId) ? this.options.bindingOf(operation.sessionId) : undefined;
    if (!binding) return FOLDER_UNAVAILABLE;
    return this.hostFor(operation.sessionId, binding).run(operation, signal);
  }

  // The guards each host checks its folder against: the binder's must be these, or the
  // sheet could accept a folder every host refuses. Without a HOME, as a host would, it throws.
  guards(): FolderGuards {
    const home = this.options.env.HOME;
    if (!home) throw new Error("the app's environment has no HOME");
    return { home, dataDir: this.options.dataDir, appDirs: this.options.appDirs ?? APP_DIRS };
  }

  stop(): Promise<void> {
    this.stopping ??= this.stopHosts();
    return this.stopping;
  }

  // The device's access ended: every host stops, and its background processes with it.
  // The next operation for a root starts a new host, which answers for them from the record.
  end(): Promise<void> {
    return this.stopHosts();
  }

  private async stopHosts(): Promise<void> {
    const hosts = [...this.live];
    this.hosts.clear();
    await Promise.all(hosts.map((host) => host.stop()));
  }

  private hostFor(root: string, binding: BoundFolder): Host {
    const known = this.hosts.get(root);
    if (known) return known;
    const { dataDir, env, bwrapPath, network } = this.options;
    const start: HostStart = {
      type: "start",
      folder: binding.folder,
      expect: { dev: binding.dev, ino: binding.ino, boot: binding.boot },
      tmp: join(dataDir, "tmp", root),
      dataDir,
      env,
      appDirs: this.options.appDirs ?? APP_DIRS,
      domains: [...(network?.granted(root) ?? [])],
      ...(bwrapPath ? { bwrapPath } : {}),
    };
    const ask = (asked: NetworkAsk, signal: AbortSignal): Promise<NetworkAnswer> =>
      network ? network.askNetwork(root, asked, signal) : Promise.resolve("deny");
    const host = new Host(
      (this.options.spawnHost ?? forkHost)(), start, this.options.startTimeoutMs ?? START_TIMEOUT_MS,
      this.options.idleMs ?? IDLE_MS, () => {
        if (this.hosts.get(root) === host) this.hosts.delete(root);
      },
      ask,
    );
    this.hosts.set(root, host);
    this.live.add(host);
    void host.exited.then(() => this.live.delete(host));
    return host;
  }
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

  constructor(
    private readonly process: HostProcess,
    start: HostStart,
    startTimeoutMs: number,
    private readonly idleMs: number,
    private readonly onGone: () => void,
    private readonly ask: (asked: NetworkAsk, signal: AbortSignal) => Promise<NetworkAnswer>,
  ) {
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

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    this.running += 1;
    clearTimeout(this.idleTimer);
    try {
      return await this.answer(operation, signal);
    } finally {
      this.running -= 1;
      this.idle();
    }
  }

  // With no operation running and no background process alive, the host keeps its
  // folder for idleMs. Out of the list first: the next operation for this root starts a new host.
  private idle(): void {
    clearTimeout(this.idleTimer);
    if (this.running > 0 || this.live > 0 || this.gone) return;
    // No command or process of the root is left, so no connection waits on its prompts.
    // ponytail: a process count that comes after its start's answer can dismiss that process's first prompt.
    this.prompts.abort();
    this.prompts = new AbortController();
    this.idleTimer = setTimeout(() => {
      this.onGone();
      void this.stop();
    }, this.idleMs).unref();
  }

  private async answer(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    // A cancel while the host is still starting is answered at once.
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
    return new Promise((resolve) => {
      const answer = (outcome: Outcome) => {
        signal.removeEventListener("abort", cancel);
        resolve(outcome);
      };
      // A newer operation may have this id by now; only this one's entry goes.
      const cancel = () => {
        if (this.pending.get(operation.id) !== answer) return;
        this.pending.delete(operation.id);
        this.send({ type: "cancel", id: operation.id });
        resolve(CANCELLED);
      };
      this.pending.set(operation.id, answer);
      signal.addEventListener("abort", cancel, { once: true });
      this.send({ type: "op", id: operation.id, kind: operation.kind, args: operation.args });
    });
  }

  async stop(): Promise<void> {
    clearTimeout(this.idleTimer);
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
      this.settleStart(null);
    } else if (message.type === "failed") {
      // A host that failed to start is exiting: the next operation starts a new one.
      this.onGone();
      this.settleStart(message.folder ? FOLDER_UNAVAILABLE : unavailable(message.message));
    } else if (message.type === "processes") {
      this.live = message.live;
      this.idle();
    } else if (message.type === "ask") {
      this.asked(message.id, { host: message.host, port: message.port, privateNetwork: message.privateNetwork });
    } else {
      const answer = this.pending.get(message.id);
      this.pending.delete(message.id);
      answer?.(message.outcome);
    }
  }

  // A destination one of its commands asked for: its host waits for this answer for
  // every connection to it. Fails closed: a choice that fails denies.
  private asked(id: number, asked: NetworkAsk): void {
    const answer = (choice: NetworkAnswer) => {
      const allow = choice === "allow" || choice === "allow_session";
      this.send({ type: "answer", id, allow, remember: choice === "allow_session" });
    };
    Promise.resolve()
      .then(() => this.ask(asked, this.prompts.signal))
      .then(answer, () => answer("deny"));
  }

  private leave(): void {
    clearTimeout(this.idleTimer);
    this.prompts.abort();
    this.gone = true;
    this.onGone();
    this.settleStart(unavailable("its tool host stopped while it was starting"));
    for (const answer of this.pending.values()) answer(HOST_STOPPED);
    this.pending.clear();
  }
}
