// A root session's side of the guest: its root runner, started in the root's
// own namespaces, and the process kinds it answers, run and which (spec,
// Section 11, "Sessions in the guest"). Every command of a root runs in its
// runner, so a server one command starts is reachable from the next.

import type { ChildProcess } from "node:child_process";

import type { Outcome } from "../link/protocol.js";
import { answered, CANCELLED, cannotEnter, type Place, ran, runArgs, SANDBOX_STOPPED, supervise, timedOut } from "./command.js";
import type { HostUser } from "./protocol.js";
import { SessionRunner } from "./runner-process.js";

export const NOT_SET_UP: Outcome = { error: { type: "unavailable", message: "This computer's sandbox has not set up this chat" } };
const ALREADY = "This chat's sandbox is already set up";
// The cloud sandbox's HOME, under which /etc/surogate/environment names the layout.
const CLOUD_HOME = /\/home\/sandbox(?=\/|:|$)/g;

// The commands' environment: the cloud's layout under the root's own HOME, and the user's names.
export function rootEnvironment(layout: string, user: HostUser): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of layout.split("\n")) {
    const at = line.indexOf("=");
    if (at > 0) env[line.slice(0, at)] = line.slice(at + 1).replace(CLOUD_HOME, user.home);
  }
  return { ...env, HOME: user.home, USER: user.name, LOGNAME: user.name, LANG: "C.UTF-8" };
}

// *answer*, or the signal, or *ms* passing, whichever comes first: the runner's
// view of the folder can stall, as a stat through virtiofs can.
function first<T>(answer: Promise<T>, signal: AbortSignal, ms?: number): Promise<T | "cancelled" | "timeout"> {
  return new Promise((resolve) => {
    const done = (value: T | "cancelled" | "timeout") => {
      clearTimeout(timer);
      signal.removeEventListener("abort", aborted);
      resolve(value);
    };
    const aborted = () => done("cancelled");
    const timer = ms === undefined ? undefined : setTimeout(() => done("timeout"), Math.min(ms, 2 ** 31 - 1));
    if (signal.aborted) return done("cancelled");
    signal.addEventListener("abort", aborted, { once: true });
    void answer.then(done);
  });
}

export class Root {
  constructor(private readonly place: Place, private readonly runner: SessionRunner) {}

  run(args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome> {
    return answered(async () => {
      const checked = runArgs(args);
      if (!("command" in checked)) return checked;
      const { folder, home } = this.place;
      const placed = await first(this.runner.ask({ type: "place", id, folder, home, workdir: checked.workdir }), signal, checked.timeout * 1000);
      if (placed === "cancelled") return CANCELLED;
      if (placed === "timeout") return timedOut(checked.timeout);
      if (!placed) return SANDBOX_STOPPED;
      if (placed.type === "refused") return { error: placed.refusal };
      if (placed.type !== "placed") return SANDBOX_STOPPED;
      if (placed.unenterable) return ran(cannotEnter(placed.unenterable, placed.cwd), -1);
      if (signal.aborted) return CANCELLED;
      const child = this.runner.spawn({ id, command: checked.command, cwd: placed.cwd, env: {}, pty: false, stdin: false });
      return supervise(child, checked.timeout, signal);
    });
  }

  async which(args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome> {
    const { name } = args;
    if (typeof name !== "string") return { error: { type: "value", message: "'name' must be a string" } };
    const found = await first(this.runner.ask({ type: "which", id, name, cwd: this.place.folder }), signal);
    if (found === "cancelled") return CANCELLED;
    return found !== "timeout" && found?.type === "found" ? { ok: found.found } : SANDBOX_STOPPED;
  }
}

export interface RootsOptions {
  // The root's runner, started in its namespaces as its own guest user; it checks what it is given first.
  start(root: string, place: Place, tag: string, user: HostUser): ChildProcess;
  // The root's guest uid.
  uid(root: string): number;
}

// The roots set up in this guest, by root session id.
export class Roots {
  private readonly roots = new Map<string, Root>();
  private readonly starting = new Set<string>();

  constructor(private readonly options: RootsOptions) {}

  uid(root: string): number {
    return this.options.uid(root);
  }

  // Rejects with why the root's runner did not start.
  async setup(root: string, folder: string, tag: string, user: HostUser): Promise<void> {
    if (this.roots.has(root) || this.starting.has(root)) throw new Error(ALREADY);
    this.starting.add(root);
    try {
      const place = { folder, home: user.home };
      const runner = new SessionRunner(this.options.start(root, place, tag, user), () => this.roots.delete(root));
      try {
        await runner.ready;
      } catch (error) {
        await runner.stop();
        throw error;
      }
      this.roots.set(root, new Root(place, runner));
    } finally {
      this.starting.delete(root);
    }
  }

  // One process operation's outcome. Never rejects.
  perform(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome> {
    const target = this.roots.get(root);
    if (!target) return Promise.resolve(NOT_SET_UP);
    if (kind === "run") return target.run(args, signal, id);
    if (kind === "which") return target.which(args, signal, id);
    return Promise.resolve({ error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } });
  }
}
