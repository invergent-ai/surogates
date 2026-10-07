import { type ChildProcess, fork } from "node:child_process";
import { statSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { BOOT_ID } from "../src/binding/folder.js";
import type { FromHost, HostStart, ToHost } from "../src/hosts/messages.js";

export const HOST = fileURLToPath(new URL("../dist/hosts/host.js", import.meta.url));
export const PACKAGE = fileURLToPath(new URL("..", import.meta.url));

// The identity a binding holds for *folder* as it is now; any, for a path that is no folder to stat.
export function bound(folder: string): HostStart["expect"] {
  try {
    const { dev, ino } = statSync(folder);
    return { dev, ino, boot: BOOT_ID };
  } catch {
    return { dev: 0, ino: 0, boot: BOOT_ID };
  }
}

export class Harness {
  readonly messages: FromHost[] = [];
  readonly child: ChildProcess;
  readonly exited: Promise<number | null>;
  stderr = "";

  // Its own process group, as forkHost makes it: srt's socat bridges are the
  // host's children and outlive a host that is killed, unless the group goes.
  // *env* is the host's own environment; omitted, it is this process's. *execPath* runs it; omitted, this process's node.
  constructor(cwd?: string, env?: NodeJS.ProcessEnv, execPath?: string) {
    this.child = fork(HOST, [], { cwd, env, ...(execPath ? { execPath } : {}), detached: true, stdio: ["ignore", "inherit", "pipe", "ipc"] });
    this.child.stderr?.on("data", (chunk: Buffer) => {
      this.stderr += chunk.toString();
    });
    this.exited = new Promise((resolve) => this.child.on("exit", (code) => resolve(code)));
    this.child.on("message", (message) => this.messages.push(message as FromHost));
    // A send to a host that has just exited fails (EPIPE): its exit is what the tests wait on.
    this.child.on("error", () => {});
  }

  killGroup(): void {
    try {
      if (this.child.pid !== undefined) process.kill(-this.child.pid, "SIGKILL");
    } catch {
      // Already gone.
    }
  }

  send(message: ToHost): void {
    this.child.send(message);
  }

  // srt removes its sockets and folders in /tmp when a host stops, never when it is
  // killed: ask it to stop, and kill the group only when it does not (or to clear
  // what is left of it, such as socat).
  async stop(timeoutMs = 3_000): Promise<void> {
    if (this.child.connected) this.send({ type: "stop" });
    const timer = setTimeout(() => this.killGroup(), timeoutMs);
    await this.exited;
    clearTimeout(timer);
    this.killGroup();
  }

  async until<T>(find: (messages: FromHost[]) => T | undefined, timeoutMs = 20_000): Promise<T> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const found = find(this.messages);
      if (found !== undefined) return found;
      if (Date.now() > deadline) throw new Error(`timed out; got ${JSON.stringify(this.messages)}`);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }

  async op(id: string, kind: string, args: Record<string, unknown>): Promise<unknown> {
    this.send({ type: "op", id, kind, args });
    return this.until((messages) => {
      const result = messages.find((message) => message.type === "result" && message.id === id);
      return result?.type === "result" ? result.outcome : undefined;
    });
  }
}
