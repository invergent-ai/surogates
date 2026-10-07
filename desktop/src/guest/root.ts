// A root session's side of the guest: its root runner, started in the root's
// own namespaces, and the process kinds it answers: run and which, and its
// background processes in its registry (spec, Section 11, "Sessions in the
// guest"). Every command of a root runs in its runner, so a server one command
// starts is reachable from the next.

import { type ChildProcess, execFile, spawn } from "node:child_process";
import { chmodSync, chownSync, mkdirSync, readdirSync, readFileSync, renameSync, rmdirSync, rmSync, statSync } from "node:fs";
import { chmod, chown, mkdir, readdir, readFile, rmdir, writeFile } from "node:fs/promises";
import { join, posix, relative } from "node:path";
import { promisify } from "node:util";

import { Failure } from "../files/answers.js";
import { inside } from "../files/paths.js";
import type { Outcome } from "../link/protocol.js";
import { answered, CANCELLED, cannotEnter, type Place, ran, runArgs, SANDBOX_STOPPED, supervise, timedOut } from "./command.js";
import { lostWith, type Placed, type ProcessHandle, Processes } from "./processes.js";
import { type Answer, type BindMode, type HostUser, MAX_SHARES, type ProtectedKey, type Question, type Share } from "./protocol.js";
import { SessionRunner } from "./runner-process.js";

// The sessions disk's folder of roots (vm/init), each named by its root session id.
export const SESSIONS = "/run/surogate/sessions/roots";
// Each share's virtiofs mount, readable by root only.
const SHARES = "/run/surogate/shares";
// Each root's cgroup, under the one vm/init bounds below the guest's memory.
export const CGROUPS = "/sys/fs/cgroup/roots";
// How many processes one root may have of the guest's 32 768.
const PIDS_MAX = 4096;
// How many cgroups one root may have below its own: its runner's, its runs' and its
// background processes', each holding at least one of its PIDS_MAX processes, and a
// cgroup per run or background process, 64 of them at most. Each costs guest kernel
// memory that no memory.max counts, and one a command made and left empty holds no
// process, which only this bounds.
const CGROUPS_MAX = 256;
// How long a root's processes have to end once its cgroup is killed. One stuck in
// a stat of a stalled share cannot end until the share answers.
const EMPTY_MS = 3_000;
const EMPTY_RETRY_MS = 10;
const ENTER_ROOT = "/run/surogate/agent/enter-root";
// Binds a root's protected keys, and the folders above them, in its namespace: on the agent
// disk, which the root's /run/surogate/agent shows too.
const PROTECT = "/run/surogate/agent/protect";
// About a millisecond a bind, measured: MAX_PROTECTED take about a second, well inside the
// host's 15 s for the request.
const PROTECT_MS = 10_000;
// How many paths a refusal names.
const NAMED = 20;
// The cloud's layout of the commands' environment, written by the image's build.
const LAYOUT = "/etc/surogate/environment";
const FIRST_UID = 10_000;
const ROOT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const TAG = /^r[0-9]{1,3}$/;
// Any name a passwd line can hold, as directories join AD users (ana@corp.example):
// no ':', newline, NUL or '/', no leading '-', at most 256 characters.
const NAME = /^(?!-)[^:\n\0/]{1,256}$/;
// What the agent keeps of every root's background processes' output together, in UTF-16
// code units: at most 32 MB, beside the agent's own memory in the 256 MiB the roots leave
// it. Each root keeps its share: the guest holds at most MAX_SHARES roots at once.
const OUTPUT_CHARS = 16_000_000;
// The background process kinds, which a root's registry answers.
const PROCESS_KINDS = new Set(["start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"]);

export const NOT_SET_UP = { error: { type: "unavailable", message: "This computer's sandbox has not set up this chat" } } satisfies Outcome;
const ALREADY = "This chat's sandbox is already set up";
const FULL = `This computer's sandbox holds ${MAX_SHARES} chats already`;
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
export async function enter(root: string, place: Place, share: Share, user: HostUser): Promise<ChildProcess> {
  if (!ROOT_ID.test(root)) throw new Error(`not a root session id: ${root}`);
  if (!TAG.test(share.tag)) throw new Error(`not a share tag: ${share.tag}`);
  if (!NAME.test(user.name)) throw new Error(`not a user name: ${user.name}`);
  checkPath(place.folder, "folder", /[\0\n]/);
  checkPath(place.home, "home folder", /[\0\n:]/);
  const uid = uidOf(root);
  const mount = await mountShare(share.tag);
  const cgroup = join(CGROUPS, root);
  // Made again for a root set up again once its runner was lost.
  await mkdir(cgroup, { recursive: true });
  // Nothing of the root runs while enter-root checks its mount points: what it ran before ends first.
  await killRoot(root).catch(() => {
    throw new Error("what this chat ran before has not ended yet");
  });
  // Then its cgroup is made again empty: the cgroups its runner and commands had go too.
  // In it: init for the runner, run for each run's cgroup, proc for each background process's.
  await removeCgroup(cgroup);
  for (const leaf of ["init", "run", "proc"]) await mkdir(join(cgroup, leaf), { recursive: true });
  await writeFile(join(cgroup, "pids.max"), String(PIDS_MAX));
  // No cgroup deeper than a command's, and at most CGROUPS_MAX.
  await writeFile(join(cgroup, "cgroup.max.descendants"), String(CGROUPS_MAX));
  await writeFile(join(cgroup, "cgroup.max.depth"), "2");
  // Delegated to the root's user, so its runner can give each run a cgroup of its own in
  // run and move its processes between the root's cgroups; it sets none of the root's own
  // limits, and makes no cgroup beside init, run and proc, nor any in proc, whose cgroups
  // the agent makes (ProcessCgroups).
  for (const path of [join(cgroup, "cgroup.procs"), join(cgroup, "run")]) await chown(path, uid, uid);
  // In the root's cgroup before unshare runs, so it and everything it starts are
  // there, and the cgroup namespace it makes is rooted there. Each by its path:
  // never through the PATH made for the commands.
  return spawn(
    "/bin/sh",
    [
      "-c", 'echo $$ > "$1/cgroup.procs" && shift && exec "$@"', "sh", cgroup,
      "/usr/bin/unshare", "--mount", "--pid", "--fork", "--kill-child", "--ipc", "--uts", "--net", "--cgroup", "--propagation", "private", "--",
      ENTER_ROOT, join(SESSIONS, root), place.folder, mount, place.home, String(uid), user.name,
    ],
    { env: rootEnvironment(readFileSync(LAYOUT, "utf8"), user), stdio: ["pipe", "pipe", "pipe"] },
  );
}

// A cgroup with every cgroup below it, each empty: a directory of cgroupfs goes by rmdir alone.
async function removeCgroup(path: string): Promise<void> {
  for (const entry of await readdir(path, { withFileTypes: true })) if (entry.isDirectory()) await removeCgroup(join(path, entry.name));
  await rmdir(path);
}

// Once *root*'s runner is up: unshare, its one process outside its namespaces, joins
// the runner's cgroup, so the root's own holds no process. Then each background
// process's memory is counted in its own cgroup, where an out-of-memory kill shows. A
// run's is not: its answer has no note, and a memory cgroup outlives its rmdir while
// pages it charged remain, as a file a run left in /tmp.
export async function contain(root: string, pid: number | undefined): Promise<void> {
  const cgroup = join(CGROUPS, root);
  await writeFile(join(cgroup, "init", "cgroup.procs"), String(pid));
  await writeFile(join(cgroup, "cgroup.subtree_control"), "+memory");
  await writeFile(join(cgroup, "proc", "cgroup.subtree_control"), "+memory");
}

/**
 * The cgroups the agent makes for a root's background processes, in the root's proc
 * folder (spec, Section 11, Cgroups), which only the agent writes. A memory cgroup a
 * command made there and removed again would hold guest kernel memory that no limit
 * counts, for as long as the page cache it charged lives. Each is the root's user's to
 * enter and to end, through its cgroup.procs and cgroup.kill, which its runner writes;
 * the agent removes it once nothing of it runs.
 */
export class ProcessCgroups {
  // Ended processes whose cgroups still held what they left: tried again at each start.
  private readonly ended = new Set<string>();

  constructor(private readonly folder: string, private readonly uid: number) {}

  // Made before the runner starts the process. Throws once the root has as many cgroups as it may.
  make(id: string): void {
    this.tidy();
    const cgroup = join(this.folder, id);
    mkdirSync(cgroup);
    for (const file of ["cgroup.procs", "cgroup.kill"]) chownSync(join(cgroup, file), this.uid, this.uid);
  }

  // Once its process has ended: removed now, or once what it left has ended too.
  end(id: string): void {
    this.ended.add(id);
    this.tidy();
  }

  private tidy(): void {
    for (const id of this.ended) {
      try {
        rmdirSync(join(this.folder, id));
        this.ended.delete(id);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") this.ended.delete(id);
      }
    }
  }
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

// What became of each path a root's namespace was asked to bind: the inode it bound, or
// absent (gone since the host looked), or failed (a link that leads nowhere).
export type Bound = Map<string, number | "absent" | "failed">;

/**
 * Each of *binds*, in order, bound over itself as its mode says, in the mount namespace of
 * the root whose unshare is *pid*, by vm/protect as the guest's root, in the root's own
 * view. A bind follows a link, as srt's deny did: a link's target is what is bound.
 */
export function bindOver(pid: number, binds: ReadonlyArray<readonly [string, BindMode]>): Promise<Bound> {
  return new Promise((resolve, reject) => {
    const child = spawn("/usr/bin/nsenter", ["--target", String(pid), "--mount", "--root", "--wd", "--", "/bin/bash", PROTECT], {
      env: { PATH: "/usr/sbin:/usr/bin:/sbin:/bin" }, stdio: ["pipe", "pipe", "ignore"],
    });
    const chunks: Buffer[] = [];
    let late = false;
    const timer = setTimeout(() => {
      late = true;
      child.kill("SIGKILL");
    }, PROTECT_MS);
    child.stdout.on("data", (chunk: Buffer) => chunks.push(chunk));
    child.stdin.on("error", () => {});
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error(late ? "it did not finish in time" : `it stopped (${code})`));
      // Each path, then its inode, nothing when absent, or ! when it could not be bound.
      const parts = Buffer.concat(chunks).toString().split("\0");
      const bound: Bound = new Map();
      for (let at = 0; at + 1 < parts.length; at += 2) {
        const said = parts[at + 1] ?? "";
        bound.set(parts[at] ?? "", said === "" ? "absent" : said === "!" ? "failed" : Number(said));
      }
      resolve(bound);
    });
    child.stdin.end(binds.map(([path, mode]) => `${mode}\0${path}\0`).join(""));
  });
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

// The refusal of a root's commands while *paths* are not bound: named relative to *folder*, at most NAMED of them.
function refusal(folder: string, paths: readonly string[], why: string): string {
  const names = paths.map((path) => relative(folder, path)).sort();
  const listed = names.length > NAMED ? `${names.slice(0, NAMED).join(", ")} and ${names.length - NAMED} more` : names.join(", ");
  return `Blocked: the computer could not make these protected files read-only in its sandbox, so commands cannot run here: ${listed}. ${why}`;
}

export class Root {
  private asked = 0;
  // Each path bound in its namespace, by the inode it had.
  private readonly bound = new Map<string, number>();
  private protecting: Promise<void> = Promise.resolve();

  constructor(
    private readonly place: Place,
    readonly runner: SessionRunner,
    private readonly lose: () => Promise<void>,
    private readonly questionMs: number,
    private readonly bind: ((binds: Array<[string, BindMode]>) => Promise<Bound>) | null = null,
  ) {}

  /**
   * *keys*, the host's list of its folder's protected keys and the folders above them now,
   * bound in its namespace: each not bound yet, or bound with another inode, and all that
   * lies in a folder bound again, since a bind of a folder holds none of the binds below
   * it. A host program that replaces one, as git config renames a new file over the old,
   * takes the bind with it. One after another; rejects with the refusal while one cannot be bound.
   */
  protect(keys: readonly ProtectedKey[]): Promise<void> {
    const done = this.protecting.then(() => this.bindKeys(keys));
    this.protecting = done.catch(() => {});
    return done;
  }

  private async bindKeys(keys: readonly ProtectedKey[]): Promise<void> {
    const { folder } = this.place;
    // Only paths in its folder are the host's to name; a folder before what lies in it.
    const named = keys.filter(([path]) => path !== folder && inside(path, folder)).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    const paths = new Set(named.map(([path]) => path));
    for (const path of this.bound.keys()) if (!paths.has(path)) this.bound.delete(path);
    const due: Array<[string, BindMode]> = [];
    for (const [path, ino, mode] of named) {
      if (this.bound.get(path) !== ino || due.some(([above]) => inside(path, above))) due.push([path, mode]);
    }
    if (!this.bind || due.length === 0) return;
    const pathsDue = due.map(([path]) => path);
    let bound: Bound;
    try {
      bound = await this.bind(due);
    } catch {
      throw new Error(refusal(folder, pathsDue, "Its sandbox did not finish binding them; the next command tries again."));
    }
    const stuck: string[] = [];
    for (const path of pathsDue) {
      const result = bound.get(path);
      if (typeof result === "number") this.bound.set(path, result);
      else if (result !== "absent") stuck.push(path);
    }
    if (stuck.length > 0) throw new Error(refusal(folder, stuck, "A link among them that leads nowhere keeps it from doing so."));
  }

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

  // Where a start's command would run, in the runner's view and as its user, as a
  // run's place: throws why not. The registry answers a cancel itself.
  async where(workdir: string | null, signal: AbortSignal): Promise<Placed> {
    const { folder, home } = this.place;
    const placed = await first(this.ask({ type: "place", id: `start-${(this.asked += 1)}`, folder, home, workdir }), signal);
    const answer = typeof placed === "object" ? placed : null;
    if (answer?.type === "refused") throw new Failure(answer.refusal);
    if (answer?.type !== "placed") throw new Failure(SANDBOX_STOPPED.error);
    return { cwd: answer.cwd, unenterable: answer.unenterable };
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
  start(root: string, place: Place, share: Share, user: HostUser): ChildProcess | Promise<ChildProcess>;
  // The root's guest uid.
  uid(root: string): number;
  // Ends every process of the root; resolves once they have all ended, or rejects.
  kill(root: string): void | Promise<void>;
  // Once the root's runner is up, the cgroup per command: *pid* is the process start gave.
  contain?(root: string, pid: number | undefined): Promise<void>;
  // Told of a root that was set up and has lost its runner: the host sets it up again.
  lost?(root: string): void;
  // Told a root's process handles to keep, and how many of its processes live, each time they change.
  handles?(root: string, handles: ProcessHandle[], live: number): void;
  // The roots' cgroups (CGROUPS): each background process gets one the agent makes in its root's proc.
  cgroups?: string;
  // Binds each path over itself as its mode says, in the namespace of the root whose unshare is *pid* (bindOver).
  protect?(pid: number, binds: Array<[string, BindMode]>): Promise<Bound>;
  questionMs?: number;
}

// The roots set up in this guest, by root session id.
export class Roots {
  private readonly roots = new Map<string, Root>();
  // The setups under way, which a teardown waits for.
  private readonly starting = new Map<string, Promise<void>>();
  // Each root's background processes, from its first setup in this guest to its
  // teardown: set up again once its runner was lost, a root still answers for
  // what ended with that runner, and how.
  private readonly registries = new Map<string, Processes>();

  constructor(private readonly options: RootsOptions) {}

  uid(root: string): number {
    return this.options.uid(root);
  }

  // Rejects with why the root's runner did not start. *ended*: the handles the host
  // keeps of the root's processes, which a root new to this guest answers for.
  async setup(root: string, folder: string, share: Share, user: HostUser, ended: readonly ProcessHandle[] = []): Promise<void> {
    if (this.roots.has(root) || this.starting.has(root)) throw new Error(ALREADY);
    // Each root set up keeps its processes' output: a share of OUTPUT_CHARS.
    const held = new Set([...this.registries.keys(), ...this.starting.keys()]);
    if (!held.has(root) && held.size >= MAX_SHARES) throw new Error(FULL);
    const started = this.start(root, folder, share, user, ended);
    this.starting.set(root, started);
    try {
      await started;
    } finally {
      this.starting.delete(root);
    }
  }

  private async start(root: string, folder: string, share: Share, user: HostUser, ended: readonly ProcessHandle[]): Promise<void> {
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
    const child = await this.options.start(root, place, share, user);
    const runner = new SessionRunner(child, () => void lose(), RUNNER_READY_MS);
    try {
      await runner.ready;
      await this.options.contain?.(root, child.pid);
    } catch (error) {
      await runner.stop();
      throw error;
    }
    const { protect } = this.options;
    const pid = child.pid ?? 0;
    listed = new Root(place, runner, lose, this.options.questionMs ?? QUESTION_MS, protect ? (binds) => protect(pid, binds) : null);
    if (!this.registries.has(root)) this.registries.set(root, this.registry(root, ended));
    this.roots.set(root, listed);
  }

  // A root's registry, its processes in whichever runner the root has set up.
  private registry(root: string, ended: readonly ProcessHandle[]): Processes {
    const current = () => {
      const target = this.roots.get(root);
      if (!target) throw new Failure(NOT_SET_UP.error);
      return target;
    };
    const cgroups = this.options.cgroups === undefined ? null : new ProcessCgroups(join(this.options.cgroups, root, "proc"), this.options.uid(root));
    const registry: Processes = new Processes({
      place: async (workdir, signal) => current().where(workdir, signal),
      runner: async () => {
        const { runner } = current();
        // Its cgroup first: the runner moves the command's shell into it before the command runs.
        return {
          spawn: (request) => {
            cgroups?.make(request.id);
            return runner.spawn(request);
          },
        };
      },
      done: (id) => cgroups?.end(id),
      keep: OUTPUT_CHARS / MAX_SHARES,
      // The host's handles: one from before the app quit comes ended as the app quit,
      // so one still running ran in a guest that went.
      ended: lostWith(ended),
      // Once the root is torn down the host keeps what it heard last: its live processes end as the app quit.
      save: (handles) => {
        if (this.registries.get(root) === registry) this.options.handles?.(root, handles, registry.live);
      },
    });
    return registry;
  }

  // *keys* read-only in *root*'s namespace; a root not set up has none, and nothing to protect. Rejects with the refusal.
  async protect(root: string, keys: readonly ProtectedKey[]): Promise<void> {
    await this.roots.get(root)?.protect(keys);
  }

  // Everything of *root* ends and it is forgotten, its share left mounted: the host
  // is letting its folder go. Its next operation sets it up again.
  async teardown(root: string): Promise<void> {
    // A setup under way lands first, and what it set up ends with the rest.
    await this.starting.get(root)?.catch(() => {});
    this.registries.delete(root);
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
    const registry = this.registries.get(root);
    if (registry && PROCESS_KINDS.has(kind)) {
      const outcome = await registry.answer(kind, args, signal);
      if (target.runner.went) await target.end();
      return outcome;
    }
    if (kind !== "run" && kind !== "which") return { error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } };
    const outcome = await (kind === "run" ? target.run(args, signal, id) : target.which(args, signal, id));
    if (outcome === SANDBOX_STOPPED) await target.end();
    return outcome;
  }
}
