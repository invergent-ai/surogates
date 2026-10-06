// A root session's side of the guest: its root runner, started in the root's
// own namespaces, and the process kinds it answers, run and which (spec,
// Section 11, "Sessions in the guest"). Every command of a root runs in its
// runner, so a server one command starts is reachable from the next.

import { type ChildProcess, execFileSync, spawn, spawnSync } from "node:child_process";
import { chmodSync, chownSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { join, posix } from "node:path";

import type { Outcome } from "../link/protocol.js";
import { answered, CANCELLED, cannotEnter, type Place, ran, runArgs, SANDBOX_STOPPED, supervise, timedOut } from "./command.js";
import type { HostUser } from "./protocol.js";
import { SessionRunner } from "./runner-process.js";

// The sessions disk's folder of roots (vm/init), each named by its root session id.
export const SESSIONS = "/run/surogate/sessions/roots";
// Each share's virtiofs mount, readable by root only.
const SHARES = "/run/surogate/shares";
const ENTER_ROOT = "/run/surogate/agent/enter-root";
// The cloud's layout of the commands' environment, written by the image's build.
const LAYOUT = "/etc/surogate/environment";
const FIRST_UID = 10_000;
const ROOT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const TAG = /^r[0-9]{1,3}$/;
// Any name a passwd line can hold, as directories join AD users (ana@corp.example):
// no ':', newline, NUL or '/', no leading '-', at most 256 characters.
const NAME = /^(?!-)[^:\n\0/]{1,256}$/;

export const NOT_SET_UP: Outcome = { error: { type: "unavailable", message: "This computer's sandbox has not set up this chat" } };
const ALREADY = "This chat's sandbox is already set up";
// The cloud sandbox's HOME, under which /etc/surogate/environment names the layout:
// at the start of a path, in a value or a list of them.
const CLOUD_HOME = /(?<=^|:)\/home\/sandbox(?=\/|:|$)/g;
// How long past its timeout a run waits for its runner to report the command's
// end. A runner that answers nothing, its loop blocked on a stalled stat or the
// runner stopped by a command, holds no run longer than its timeout and this.
const GRACE_MS = 2_000;

// The commands' environment: the cloud's layout under the root's own HOME, and the user's names.
export function rootEnvironment(layout: string, user: HostUser): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of layout.split("\n")) {
    const at = line.indexOf("=");
    // A function, so a '$' in the home is not read as a replacement pattern.
    if (at > 0) env[line.slice(0, at)] = line.slice(at + 1).replace(CLOUD_HOME, () => user.home);
  }
  return { ...env, HOME: user.home, USER: user.name, LOGNAME: user.name, LANG: "C.UTF-8" };
}

// The guest uid of *root*: given at its first ask, from FIRST_UID up, and kept
// as the owner of its folder on the sessions disk, which holds its home and temp
// folder. A uid is never given again while that folder exists.
export function uidOf(root: string, sessions = SESSIONS): number {
  if (!ROOT_ID.test(root)) throw new Error(`not a root session id: ${root}`);
  const folder = join(sessions, root);
  try {
    return statSync(folder).uid;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const taken = readdirSync(sessions).filter((name) => ROOT_ID.test(name)).map((name) => statSync(join(sessions, name)).uid);
  const uid = Math.max(FIRST_UID - 1, ...taken) + 1;
  // Made whole under another name, then renamed: the folder is never there with another owner.
  const making = join(sessions, `.${root}`);
  rmSync(making, { recursive: true, force: true });
  for (const [path, mode] of [[making, 0o700], [join(making, "home"), 0o700], [join(making, "tmp"), 0o1777]] as const) {
    mkdirSync(path);
    chownSync(path, uid, uid);
    chmodSync(path, mode);
  }
  renameSync(making, folder);
  return uid;
}

function checkPath(path: string, what: string): void {
  if (!path.startsWith("/") || path === "/" || path.includes("\0") || posix.normalize(path) !== path) {
    throw new Error(`not a ${what}: ${path}`);
  }
}

// The root's runner in its own namespaces: mount, PID, IPC, UTS, network and
// cgroup, as util-linux's unshare makes them, and enter-root builds them. Ending
// the runner's stdin ends the runner, then tini, the namespaces' PID 1, and
// everything in them with it. Killing unshare reaches them only while enter-root
// builds them: the kernel drops the parent-death signal when the runner takes on
// the root's user. Nothing is made or mounted until every input has passed.
export function enter(root: string, place: Place, tag: string, user: HostUser): ChildProcess {
  if (!ROOT_ID.test(root)) throw new Error(`not a root session id: ${root}`);
  if (!TAG.test(tag)) throw new Error(`not a share tag: ${tag}`);
  if (!NAME.test(user.name)) throw new Error(`not a user name: ${user.name}`);
  checkPath(place.folder, "folder");
  checkPath(place.home, "home folder");
  const uid = uidOf(root);
  const share = join(SHARES, tag);
  mkdirSync(share, { recursive: true });
  chmodSync(SHARES, 0o700);
  if (spawnSync("mountpoint", ["-q", share]).status !== 0) execFileSync("mount", ["-t", "virtiofs", tag, share]);
  return spawn(
    "unshare",
    [
      "--mount", "--pid", "--fork", "--kill-child", "--ipc", "--uts", "--net", "--cgroup", "--propagation", "private", "--",
      ENTER_ROOT, join(SESSIONS, root), place.folder, share, place.home, String(uid), user.name,
    ],
    { env: rootEnvironment(readFileSync(LAYOUT, "utf8"), user), stdio: ["pipe", "pipe", "pipe"] },
  );
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
      // One for the whole run, its folder lookup and its command.
      const deadline = Date.now() + checked.timeout * 1000 + GRACE_MS;
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
      // The runner's report of its end, or the cancel, or the deadline. The kill
      // waits in the runner's input, and a report after the answer settles nothing.
      const ended = await first(supervise(child, checked.timeout, signal), signal, deadline - Date.now());
      if (ended === "cancelled") return CANCELLED;
      if (ended !== "timeout") return ended;
      child.kill();
      return timedOut(checked.timeout);
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
