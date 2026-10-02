// Runs each operation on its root session's folder: one tool host per root,
// started on that root's first operation (spec, Section 1). The folder comes from
// the app's own record of the binding, never from the request.

import { fork } from "node:child_process";
import { statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "./messages.js";

// The same from src/hosts and from dist/hosts.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
const HOST = join(PACKAGE, "dist", "hosts", "host.js");
const STOP_TIMEOUT_MS = 5_000;
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

function isFolder(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

export interface Binding {
  folder: string;
}

export interface HostProcess {
  send(message: ToHost): void;
  onMessage(listener: (message: FromHost) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
}

export function forkHost(): HostProcess {
  // Its own process group: srt's socat bridges are the host's children, outside
  // the sandbox, and do not die with it, so a host that goes takes its group
  // along. Its stdout goes to stderr: the app's stdout may carry other things.
  const child = fork(HOST, [], { detached: true, stdio: ["ignore", 2, 2, "ipc"] });
  const killGroup = () => {
    try {
      if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
    } catch {
      // The group has gone.
    }
  };
  // A send to a host that has gone: its exit is what counts.
  child.on("error", () => {});
  child.on("exit", killGroup);
  return {
    send: (message) => {
      child.send(message);
    },
    onMessage: (listener) => {
      child.on("message", (message) => listener(message as FromHost));
    },
    onExit: (listener) => {
      child.on("exit", () => listener());
    },
    kill: killGroup,
  };
}

export interface ToolHostsOptions {
  bindingOf(rootSessionId: string): Binding | undefined;
  dataDir: string;
  env: Record<string, string>;
  appDirs?: string[];
  bwrapPath?: string;
  spawnHost?: () => HostProcess;
}

export class ToolHosts implements Executor {
  private readonly hosts = new Map<string, Host>();
  private stopped = false;

  constructor(private readonly options: ToolHostsOptions) {}

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (operation.kind === "bind") return NOT_BOUND;
    if (this.stopped) return unavailable("the app is quitting");
    const binding = SESSION_ID.test(operation.sessionId) ? this.options.bindingOf(operation.sessionId) : undefined;
    if (!binding || !isFolder(binding.folder)) return FOLDER_UNAVAILABLE;
    return this.hostFor(operation.sessionId, binding).run(operation, signal);
  }

  async stop(): Promise<void> {
    this.stopped = true;
    const hosts = [...this.hosts.values()];
    this.hosts.clear();
    await Promise.all(hosts.map((host) => host.stop()));
  }

  private hostFor(root: string, binding: Binding): Host {
    const known = this.hosts.get(root);
    if (known) return known;
    const { dataDir, env, bwrapPath } = this.options;
    const start: HostStart = {
      type: "start",
      folder: binding.folder,
      tmp: join(dataDir, "tmp", root),
      dataDir,
      env,
      appDirs: this.options.appDirs ?? [dirname(process.execPath), PACKAGE],
      ...(bwrapPath ? { bwrapPath } : {}),
    };
    const host = new Host((this.options.spawnHost ?? forkHost)(), start, () => {
      if (this.hosts.get(root) === host) this.hosts.delete(root);
    });
    this.hosts.set(root, host);
    return host;
  }
}

class Host {
  private readonly pending = new Map<string, (outcome: Outcome) => void>();
  private readonly started: Promise<Outcome | null>;
  private readonly exited: Promise<void>;
  private settleStart: (failure: Outcome | null) => void = () => {};
  private gone = false;

  constructor(private readonly process: HostProcess, start: HostStart, private readonly onGone: () => void) {
    this.started = new Promise((resolve) => {
      this.settleStart = resolve;
    });
    this.exited = new Promise((resolve) => {
      process.onExit(() => {
        this.leave();
        resolve();
      });
    });
    process.onMessage((message) => this.received(message));
    process.send(start);
  }

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const failure = await this.started;
    if (failure) return failure;
    if (this.gone) return unavailable("its tool host stopped; try again");
    if (signal.aborted) return CANCELLED;
    return new Promise((resolve) => {
      this.pending.set(operation.id, resolve);
      signal.addEventListener("abort", () => {
        if (!this.pending.delete(operation.id)) return;
        this.send({ type: "cancel", id: operation.id });
        resolve(CANCELLED);
      }, { once: true });
      this.send({ type: "op", id: operation.id, kind: operation.kind, args: operation.args });
    });
  }

  async stop(): Promise<void> {
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
      this.settleStart(unavailable(message.message));
    } else {
      const resolve = this.pending.get(message.id);
      this.pending.delete(message.id);
      resolve?.(message.outcome);
    }
  }

  private leave(): void {
    this.gone = true;
    this.onGone();
    this.settleStart(unavailable("its tool host stopped while it was starting"));
    for (const resolve of this.pending.values()) resolve(HOST_STOPPED);
    this.pending.clear();
  }
}
