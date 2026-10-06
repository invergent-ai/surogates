// A root session's side of the guest: its root runner, started in the root's
// own namespaces, and the process kinds it answers, run and which (spec,
// Section 11, "Sessions in the guest"). Every command of a root runs in its
// runner, so a server one command starts is reachable from the next.

import { type ChildProcess, execFile, spawn } from "node:child_process";
import { chmodSync, chownSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { join, posix } from "node:path";
import { promisify } from "node:util";

import type { Outcome } from "../link/protocol.js";
import { answered, CANCELLED, cannotEnter, type Place, ran, runArgs, SANDBOX_STOPPED, supervise, timedOut } from "./command.js";
import type { Answer, HostUser, Question } from "./protocol.js";
import { SessionRunner } from "./runner-process.js";

// The sessions disk's folder of roots (vm/init), each named by its root session id.
export const SESSIONS = "/run/surogate/sessions/roots";
// Each share's virtiofs mount, readable by root only.
const SHARES = "/run/surogate/shares";
// Each root's cgroup, under the one vm/init bounds below the guest's memory.
const CGROUPS = "/sys/fs/cgroup/roots";
// How many processes one root may have of the guest's 32 768.
const PIDS_MAX = 4096;
// How long a root's processes have to end once its cgroup is killed. One stuck in
// a stat of a stalled share cannot end until the share answers.
const EMPTY_MS = 3_000;
const EMPTY_RETRY_MS = 10;
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
// A share the host has just added is there once the guest's kernel has found its
// device, within about 50 ms: until then its mount fails, and is tried again.
const MOUNT_MS = 5_000;
const MOUNT_RETRY_MS = 25;
// A root's runner starts in about 50 ms. The host gives a setup 15 s, past this, MOUNT_MS and EMPTY_MS.
const RUNNER_READY_MS = 5_000;
// A runner that answers no question in this long is stopped, by one of its own
// commands, or stuck in a stat of the folder that does not return: it is lost.
const QUESTION_MS = 10_000;
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
    const { uid } = statSync(folder);
    // Only this agent makes these folders, each owned from FIRST_UID up: any other owner was never given here.
    if (uid < FIRST_UID) throw new Error(`the folder of ${root} on the sessions disk is not one this guest made`);
    return uid;
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

// A trailing '/' would keep every key out of the folder; a newline would split the
// shell's and passwd's lines, as a ':' in the home would split its passwd line.
function checkPath(path: string, what: string, forbidden: RegExp): void {
  if (!path.startsWith("/") || path === "/" || path.endsWith("/") || forbidden.test(path) || posix.normalize(path) !== path) {
    throw new Error(`not a ${what}: ${path}`);
  }
}

const execute = promisify(execFile);
// The shares mounted in this guest, by tag. A share is mounted once and never
// looked at from here again: a stat on a stalled one would stall every root.
const mounted = new Set<string>();

async function mountShare(tag: string): Promise<string> {
  const share = join(SHARES, tag);
  if (mounted.has(tag)) return share;
  await mkdir(share, { recursive: true });
  await chmod(SHARES, 0o700);
  for (const deadline = Date.now() + MOUNT_MS; ;) {
    try {
      await execute("/usr/bin/mount", ["-t", "virtiofs", "-o", "nosuid,nodev", tag, share]);
      break;
    } catch (error) {
      if (Date.now() > deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, MOUNT_RETRY_MS));
    }
  }
  mounted.add(tag);
  return share;
}

// The root's runner in its own namespaces: mount, PID, IPC, UTS, network and
// cgroup, as util-linux's unshare makes them, and enter-root builds them. Ending
// the runner's stdin ends the runner, then tini, the namespaces' PID 1, and
// everything in them with it. Killing unshare reaches them only while enter-root
// builds them: the kernel drops the parent-death signal when the runner takes on
// the root's user. Nothing is made, mounted or given until every input has passed.
export async function enter(root: string, place: Place, tag: string, user: HostUser): Promise<ChildProcess> {
  if (!ROOT_ID.test(root)) throw new Error(`not a root session id: ${root}`);
  if (!TAG.test(tag)) throw new Error(`not a share tag: ${tag}`);
  if (!NAME.test(user.name)) throw new Error(`not a user name: ${user.name}`);
  checkPath(place.folder, "folder", /[\0\n]/);
  checkPath(place.home, "home folder", /[\0\n:]/);
  const uid = uidOf(root);
  const share = await mountShare(tag);
  const cgroup = join(CGROUPS, root);
  // Made again for a root set up again once its runner was lost.
  await mkdir(cgroup, { recursive: true });
  // Nothing of the root runs while enter-root checks its mount points: what it ran before ends first.
  await killRoot(root).catch(() => {
    throw new Error("what this chat ran before has not ended yet");
  });
  await writeFile(join(cgroup, "pids.max"), String(PIDS_MAX));
  // In the root's cgroup before unshare runs, so it and everything it starts are
  // there, and the cgroup namespace it makes is rooted there. Each by its path:
  // never through the PATH made for the commands.
  return spawn(
    "/bin/sh",
    [
      "-c", 'echo $$ > "$1/cgroup.procs" && shift && exec "$@"', "sh", cgroup,
      "/usr/bin/unshare", "--mount", "--pid", "--fork", "--kill-child", "--ipc", "--uts", "--net", "--cgroup", "--propagation", "private", "--",
      ENTER_ROOT, join(SESSIONS, root), place.folder, share, place.home, String(uid), user.name,
    ],
    { env: rootEnvironment(readFileSync(LAYOUT, "utf8"), user), stdio: ["pipe", "pipe", "pipe"] },
  );
}

// Everything of *root* ends at once, its runner and the namespaces' PID 1 among it,
// whatever its uid or state. Resolves once its cgroup is empty, or rejects if it is not by EMPTY_MS.
export async function killRoot(root: string): Promise<void> {
  if (!ROOT_ID.test(root)) return;
  const cgroup = join(CGROUPS, root);
  await writeFile(join(cgroup, "cgroup.kill"), "1");
  for (const deadline = Date.now() + EMPTY_MS; !/^populated 0$/m.test(await readFile(join(cgroup, "cgroup.events"), "utf8"));) {
    if (Date.now() > deadline) throw new Error(`the processes of ${root} have not ended`);
    await new Promise((resolve) => setTimeout(resolve, EMPTY_RETRY_MS));
  }
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
  constructor(
    private readonly place: Place,
    private readonly runner: SessionRunner,
    private readonly lose: () => Promise<void>,
    private readonly questionMs: number,
  ) {}

  // Everything of the root ends; resolves once it has, and its runner has gone.
  async end(): Promise<void> {
    await this.lose();
    await this.runner.gone;
  }

  // The runner's answer, or null once it has gone; one that does not come in questionMs loses the root.
  private ask(question: Question): Promise<Answer | null> {
    const answer = this.runner.ask(question);
    const timer = setTimeout(this.lose, this.questionMs).unref();
    void answer.then(() => clearTimeout(timer));
    return answer;
  }

  run(args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome> {
    return answered(async () => {
      const checked = runArgs(args);
      if (!("command" in checked)) return checked;
      // One for the whole run, its folder lookup and its command.
      const deadline = Date.now() + checked.timeout * 1000 + GRACE_MS;
      const { folder, home } = this.place;
      const placed = await first(this.ask({ type: "place", id, folder, home, workdir: checked.workdir }), signal, checked.timeout * 1000);
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
    const found = await first(this.ask({ type: "which", id, name, cwd: this.place.folder }), signal);
    if (found === "cancelled") return CANCELLED;
    if (found !== "timeout" && found?.type === "found") return { ok: found.found };
    if (found !== "timeout" && found?.type === "refused") return { error: found.refusal };
    return SANDBOX_STOPPED;
  }
}

export interface RootsOptions {
  // The root's runner, started in its namespaces as its own guest user; it checks what it is given first.
  start(root: string, place: Place, tag: string, user: HostUser): ChildProcess | Promise<ChildProcess>;
  // The root's guest uid.
  uid(root: string): number;
  // Ends every process of the root; resolves once they have all ended, or rejects.
  kill(root: string): void | Promise<void>;
  // Told of a root that was set up and has lost its runner: the host sets it up again.
  lost?(root: string): void;
  questionMs?: number;
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
      let listed: Root | undefined;
      let ending: Promise<void> | undefined;
      // Everything of the root ends, once however often asked: its cgroup is killed
      // and emptied or, where it cannot be, its runner's stdin is ended. Only then is
      // a root still listed forgotten and the host told, so its setup again finds nothing of it running.
      const lose = () => (ending ??= (async () => {
        try {
          await this.options.kill(root);
        } catch {
          await runner.stop();
        }
        if (listed && this.roots.get(root) === listed) {
          this.roots.delete(root);
          this.options.lost?.(root);
        }
      })());
      const runner = new SessionRunner(await this.options.start(root, place, tag, user), () => void lose(), RUNNER_READY_MS);
      try {
        await runner.ready;
      } catch (error) {
        await runner.stop();
        throw error;
      }
      listed = new Root(place, runner, lose, this.options.questionMs ?? QUESTION_MS);
      this.roots.set(root, listed);
    } finally {
      this.starting.delete(root);
    }
  }

  // Everything of *root* ends and it is forgotten, its share left mounted: the host
  // is letting its folder go. Its next operation sets it up again.
  async teardown(root: string): Promise<void> {
    const target = this.roots.get(root);
    if (!target) return;
    // Out of the list first: the end of its runner is no loss to tell.
    this.roots.delete(root);
    await target.end();
  }

  // One process operation's outcome. Never rejects. A root whose runner cannot answer
  // is lost: what waited on it is answered once all of it has ended, the host told first.
  async perform(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome> {
    const target = this.roots.get(root);
    if (!target) return NOT_SET_UP;
    if (kind !== "run" && kind !== "which") return { error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } };
    const outcome = await (kind === "run" ? target.run(args, signal, id) : target.which(args, signal, id));
    if (outcome === SANDBOX_STOPPED) await target.end();
    return outcome;
  }
}
