// A root session's side of the guest: its root runner, started in the root's
// own namespaces, and the process kinds it answers: run and which, and its
// background processes in its registry (spec, Section 11, "Sessions in the
// guest"). Every command of a root runs in its runner, so a server one command
// starts is reachable from the next.

import { type ChildProcess, execFile, spawn } from "node:child_process";
import { chmodSync, chownSync, mkdirSync, readdirSync, readFileSync, renameSync, rmdirSync, rmSync, statSync } from "node:fs";
import { chmod, chown, mkdir, readdir, readFile, rmdir, writeFile } from "node:fs/promises";
import { join, posix } from "node:path";
import { promisify } from "node:util";

import { Failure } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import { answered, CANCELLED, cannotEnter, type Place, ran, runArgs, SANDBOX_STOPPED, supervise, timedOut } from "./command.js";
import { PROXY_URL } from "./listeners.js";
import { socketOf } from "./network.js";
import { lostWith, type Placed, type ProcessHandle, Processes } from "./processes.js";
import { type Answer, type HostUser, MAX_SHARES, type Question, ROOT_ID, type Share } from "./protocol.js";
import { SessionRunner } from "./runner-process.js";

// The sessions disk (vm/init), and its folder of roots, each named by its root session id.
const SESSIONS_DISK = "/run/surogate/sessions";
export const SESSIONS = `${SESSIONS_DISK}/roots`;
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
// How long a shutdown waits for every root's processes, killed together, to end: inside the
// host's 5 s from its shutdown to the guest's power-off.
const KILLED_MS = 1_000;
// How long a flush of a share waits for its server: one that has not answered by then stalled.
// A teardown's fits the host's 15 s with EMPTY_MS; the stop's, its 5 s with KILLED_MS.
const FLUSH_MS = 3_000;
const ENTER_ROOT = "/run/surogate/agent/enter-root";
// The cloud's layout of the commands' environment, written by the image's build.
const LAYOUT = "/etc/surogate/environment";
const FIRST_UID = 10_000;
// A share's number in its guest is never used again there, so it grows with every folder added.
const TAG = /^r[1-9][0-9]{0,8}$/;
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
const HELD = "What this chat ran is waiting on its folder, which does not answer";
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
// How far past a command's timeout its backstop in the guest falls (Backstop).
const BACKSTOP_MS = 10_000;
const MAX_TIMER_MS = 2 ** 31 - 1;

// The commands' environment: the cloud's layout under the root's own HOME, and the user's names.
// Their proxy variables name the root's runner's proxies, the commands' one way out; ALL_PROXY
// too, as srt set it, since httpx fails at once on a socks5h one without socksio; and the
// root's own loopback stays direct, so a session's servers are reached as they are: by the
// address a server says it listens on (0.0.0.0) and by the root's own name (enter-root's) too.
// And git compares no owner, inode or sub-second time in its index (core.checkStat=minimal),
// through git's own environment, after any the layout gives: the folder's files are the root's
// uid in the guest and the user's on the host, so git in the guest would otherwise rehash every
// file after any git on the host refreshed the index. The user's repository config is untouched.
export function rootEnvironment(layout: string, user: HostUser): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of layout.split("\n")) {
    const at = line.indexOf("=");
    // A function, so a '$' in the home is not read as a replacement pattern.
    if (at > 0) env[line.slice(0, at)] = line.slice(at + 1).replace(CLOUD_HOME, () => user.home);
  }
  const git = Number(env.GIT_CONFIG_COUNT ?? 0);
  const proxies = Object.fromEntries(["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"].flatMap((name) => [[name, PROXY_URL], [name.toLowerCase(), PROXY_URL]]));
  const direct = "localhost,127.0.0.1,::1,0.0.0.0,surogate";
  return {
    ...env, ...proxies, NO_PROXY: direct, no_proxy: direct, HOME: user.home, USER: user.name, LOGNAME: user.name, LANG: "C.UTF-8",
    GIT_CONFIG_COUNT: String(git + 1), [`GIT_CONFIG_KEY_${git}`]: "core.checkStat", [`GIT_CONFIG_VALUE_${git}`]: "minimal",
  };
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
  for (const deadline = performance.now() + MOUNT_MS; ;) {
    try {
      await execute("/usr/bin/mount", ["-t", "virtiofs", "-o", "nosuid,nodev", tag, share]);
      break;
    } catch (error) {
      if (performance.now() > deadline) throw error;
      await new Promise((resolve) => setTimeout(resolve, MOUNT_RETRY_MS));
    }
  }
  mounted.add(tag);
  return share;
}

// A share's mount goes, lazily: a process of its root's stuck in a share that stalled
// keeps it until it lets go. The host removes the share from the guest next.
export async function unmountShare(share: Share): Promise<void> {
  if (!TAG.test(share.tag)) return;
  mounted.delete(share.tag);
  const path = join(SHARES, share.tag);
  await execute("/usr/bin/umount", ["-l", path]).catch(() => {});
  await rmdir(path).catch(() => {});
}

// *path*'s filesystem written out, that one alone (syncfs, as sync -f does): never sync(2),
// which writes out every filesystem in the guest, and waits for good on a share that stalled.
// True once it has been, false if it has not within *ms*, its sync left waiting.
function flush(path: string, ms = Infinity): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = ms === Infinity ? undefined : setTimeout(() => resolve(false), ms);
    execFile("/usr/bin/sync", ["-f", path], (error) => {
      clearTimeout(timer);
      resolve(!error);
    });
  });
}

/**
 * What a root torn down wrote, written out: its home's and /tmp's on the sessions disk, and its
 * folder's through its share, if the share was mounted and its root's processes all ended. False
 * when the share did not answer within FLUSH_MS: it stalled, and is held.
 */
export async function flushRoot(share: Share, stalled: boolean): Promise<boolean> {
  const shared = !stalled && TAG.test(share.tag) && mounted.has(share.tag);
  const [, answered] = await Promise.all([flush(SESSIONS_DISK), shared ? flush(join(SHARES, share.tag), FLUSH_MS) : true]);
  return answered;
}

/**
 * The guest's stop: every root's processes end at once, every share and the sessions disk are
 * written out, each alone, and the guest powers off. Only the sessions disk could lose writes to
 * a power cut: the image and the agent disk are read-only, and the shares are written through.
 */
export async function powerOff(): Promise<void> {
  await writeFile(join(CGROUPS, "cgroup.kill"), "1").catch(() => {});
  // Killed, they end at once; one waiting on a share that stalled cannot, and the guest powers off around it.
  await emptied(CGROUPS, KILLED_MS).catch(() => {});
  // A share that stalled answers no flush: it is left to its bound.
  await Promise.all([...mounted].map((tag) => flush(join(SHARES, tag), KILLED_MS)));
  // Written out first: a root whose process cannot end still holds the disk in its own
  // namespace, so the unmount here may leave it mounted there. Unmounted by its last holder,
  // it is clean, and the next boot's check has nothing to replay.
  await flush(SESSIONS_DISK);
  await execute("/usr/bin/umount", [SESSIONS_DISK]).catch(() => {});
  await writeFile("/proc/sysrq-trigger", "o");
}

// The guest's clock set to *now*, milliseconds since the epoch.
export async function setClock(now: number): Promise<void> {
  await execute("/usr/bin/date", ["-u", "-s", `@${(now / 1000).toFixed(3)}`]);
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
  // Made at the root's first setup in this guest, and kept until the guest stops: the root's
  // own memory cgroup, the only one it has. Removed while pages it charged remain, as a
  // file a command left in /tmp, it would linger dying, counted by no limit.
  await mkdir(cgroup, { recursive: true });
  // Nothing of the root runs while enter-root checks its mount points: what it ran before ends first.
  await killRoot(root).catch(() => {
    throw new Error("what this chat ran before has not ended yet");
  });
  // Then the cgroups its runner and commands had below it go, and are made again empty:
  // init for the runner, run for each run's cgroup, proc for each background process's.
  // None of them counts memory, so none lingers once removed.
  for (const leaf of ["init", "run", "proc"]) {
    await removeCgroup(join(cgroup, leaf)).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") throw error;
    });
    await mkdir(join(cgroup, leaf));
  }
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
      ENTER_ROOT, join(SESSIONS, root), place.folder, mount, place.home, String(uid), user.name, socketOf(root),
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
// the runner's cgroup, so the root's own holds no process. The root's memory is counted
// in its own cgroup alone, where an out-of-memory kill shows (runner.ts): no cgroup below
// it counts memory, so its next setup can enter it again, and none lingers once removed.
export async function contain(root: string, pid: number | undefined): Promise<void> {
  await writeFile(join(CGROUPS, root, "init", "cgroup.procs"), String(pid));
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
  await emptied(cgroup).catch(() => {
    throw new Error(`the processes of ${root} have not ended`);
  });
}

// Resolves once *cgroup* holds no process, or rejects if it still does after *ms*.
async function emptied(cgroup: string, ms = EMPTY_MS): Promise<void> {
  for (const deadline = performance.now() + ms; !/^populated 0$/m.test(await readFile(join(cgroup, "cgroup.events"), "utf8"));) {
    if (performance.now() > deadline) throw new Error("not emptied");
    await new Promise((resolve) => setTimeout(resolve, EMPTY_RETRY_MS));
  }
}

// *answer*, or the signal, or *late*, whichever comes first: the runner's view of the
// folder can stall, as a stat through virtiofs can.
function first<T>(answer: Promise<T>, signal: AbortSignal, late?: Promise<"timeout">): Promise<T | "cancelled" | "timeout"> {
  return new Promise((resolve) => {
    const done = (value: T | "cancelled" | "timeout") => {
      signal.removeEventListener("abort", aborted);
      resolve(value);
    };
    const aborted = () => done("cancelled");
    if (signal.aborted) return done("cancelled");
    signal.addEventListener("abort", aborted, { once: true });
    void answer.then(done);
    void late?.then(done);
  });
}

// What a run's backstop knows of the host: how long it has said nothing, and when it next speaks.
export interface HostHeard {
  silentFor(): number;
  next(): Promise<void>;
}

/**
 * A run's deadline in the guest, a backstop only (spec, Section 11, Lifecycle): this computer
 * keeps the command's timeout on its own monotonic clock, which does not count its sleep, and
 * ends the command there (vm/manager.ts, Guest.op). The backstop falls *margin* past that
 * timeout, later by each time the computer slept (extend), and never while the host has been
 * silent past *silence*: a guest whose clock counted the sleep wakes to find it due, and waits
 * to hear the host, which ends the command itself if its time has come, then gives it the margin again.
 */
export class Backstop {
  readonly fell: Promise<"timeout">;
  private due: number;
  private timer: NodeJS.Timeout | undefined;
  private fall: () => void = () => {};
  private ended = false;

  constructor(ms: number, private readonly margin: number, private readonly host: HostHeard | null, private readonly silence: number) {
    this.due = performance.now() + ms + margin;
    this.fell = new Promise((resolve) => {
      this.fall = () => resolve("timeout");
    });
    this.arm();
  }

  extend(ms: number): void {
    this.due += ms;
    this.arm();
  }

  end(): void {
    this.ended = true;
    clearTimeout(this.timer);
  }

  private arm(): void {
    clearTimeout(this.timer);
    if (this.ended) return;
    this.timer = setTimeout(() => this.reached(), Math.min(Math.max(0, this.due - performance.now()), MAX_TIMER_MS));
  }

  private reached(): void {
    if (this.host && this.host.silentFor() > this.silence) {
      void this.host.next().then(() => {
        this.due = Math.max(this.due, performance.now() + this.margin);
        this.arm();
      });
      return;
    }
    if (performance.now() < this.due) return this.arm();
    this.fall();
  }
}

export class Root {
  private asked = 0;

  constructor(
    private readonly place: Place,
    readonly runner: SessionRunner,
    private readonly lose: () => Promise<void>,
    private readonly questionMs: number,
    // A run's backstop, falling past its timeout of *ms* (Roots.backstop).
    private readonly backstop: (ms: number) => Backstop,
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
      const backstop = this.backstop(checked.timeout * 1000);
      try {
        const { folder, home } = this.place;
        const placed = await first(this.ask({ type: "place", id, folder, home, workdir: checked.workdir }), signal, backstop.fell);
        if (placed === "cancelled") return CANCELLED;
        if (placed === "timeout") return timedOut(checked.timeout);
        if (!placed) return SANDBOX_STOPPED;
        if (placed.type === "refused") return { error: placed.refusal };
        if (placed.type !== "placed") return SANDBOX_STOPPED;
        if (placed.unenterable) return ran(cannotEnter(placed.unenterable, placed.cwd), -1);
        if (signal.aborted) return CANCELLED;
        const child = this.runner.spawn({ id, command: checked.command, cwd: placed.cwd, env: {}, pty: false, stdin: false });
        // The runner's report of its end, or the cancel, or the backstop. The kill
        // waits in the runner's input, and a report after the answer settles nothing.
        const ended = await first(supervise(child, signal), signal, backstop.fell);
        if (ended === "cancelled") return CANCELLED;
        if (ended !== "timeout") return ended;
        child.kill();
        return timedOut(checked.timeout);
      } finally {
        backstop.end();
      }
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
  // The mount of a share whose root was torn down goes (unmountShare).
  unmount?(share: Share): Promise<void>;
  // What a root torn down wrote, written out, its share's too unless it *stalled*: false when the share did not answer (flushRoot).
  flush?(share: Share, stalled: boolean): Promise<boolean>;
  // The root's socket for its connections to the host proxy, its guest user's, made before its namespaces (Network.listen); resolves with what closes it.
  tunnels?(root: string, uid: number): Promise<() => void>;
  questionMs?: number;
  // How far past a run's timeout its backstop falls: BACKSTOP_MS by default.
  backstopMs?: number;
  // How long the host may say nothing before a backstop waits to hear it; without it, none waits (the tests).
  hostSilenceMs?: number;
}

// The roots set up in this guest, by root session id.
export class Roots {
  private readonly roots = new Map<string, Root>();
  // The setups under way, which a teardown waits for.
  private readonly starting = new Map<string, Promise<void>>();
  // What of each root is ending, a teardown or a lost runner's end, to its last step: a
  // setup waits for it, so none of it lands on what it sets up, its socket or its cgroup.
  private readonly endings = new Map<string, Promise<void>>();
  // Each root's background processes, from its first setup in this guest to its
  // teardown: set up again once its runner was lost, a root still answers for
  // what ended with that runner, and how.
  private readonly registries = new Map<string, Processes>();
  // The roots whose end left something running that would not end.
  private readonly held = new Set<string>();
  // Every run's backstop in this guest, and when the host was last heard, and who waits to hear it next.
  private readonly backstops = new Set<Backstop>();
  private heardAt = performance.now();
  private readonly hearing = new Set<() => void>();
  private readonly host: HostHeard = {
    silentFor: () => performance.now() - this.heardAt,
    next: () => new Promise((resolve) => void this.hearing.add(resolve)),
  };

  constructor(private readonly options: RootsOptions) {}

  /** The host spoke: a backstop that waited to hear it goes on. */
  heard(): void {
    this.heardAt = performance.now();
    for (const go of this.hearing) go();
    this.hearing.clear();
  }

  /** The computer slept *ms*: every run's backstop falls that much later. */
  woke(ms: number): void {
    for (const backstop of this.backstops) backstop.extend(ms);
  }

  // A run's backstop, kept in the set until it ends.
  private backstop(ms: number): Backstop {
    const silence = this.options.hostSilenceMs;
    const made = new Backstop(ms, this.options.backstopMs ?? BACKSTOP_MS, silence === undefined ? null : this.host, silence ?? 0);
    this.backstops.add(made);
    const end = made.end.bind(made);
    made.end = () => {
      this.backstops.delete(made);
      end();
    };
    return made;
  }

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
    // Under way while it waits: a teardown waits for it, and a second setup is refused.
    const ending = this.endings.get(root);
    const started = ending ? ending.then(() => this.start(root, folder, share, user, ended)) : this.start(root, folder, share, user, ended);
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
    // Its namespaces bind it, so it is there before they are made, and goes with everything of the root.
    const unlisten = (await this.options.tunnels?.(root, this.options.uid(root))) ?? (() => {});
    // Everything of the root ends, once however often asked: its cgroup is killed
    // and emptied or, where it cannot be, its runner's stdin is ended. Only then is
    // a root still listed forgotten and the host told, so its setup again finds nothing of it running.
    const lose = () => (ending ??= this.ends(root, (async () => {
      try {
        await this.options.kill(root);
      } catch {
        // Something of it would not end: its share stays held until its teardown says so.
        this.held.add(root);
        await runner.stop();
      }
      unlisten();
      if (listed && this.roots.get(root) === listed) {
        this.roots.delete(root);
        this.options.lost?.(root);
      }
    })()));
    let child: ChildProcess;
    try {
      child = await this.options.start(root, place, share, user);
    } catch (error) {
      unlisten();
      throw error;
    }
    const runner = new SessionRunner(child, () => void lose(), RUNNER_READY_MS);
    try {
      await runner.ready;
      await this.options.contain?.(root, child.pid);
    } catch (error) {
      await runner.stop();
      unlisten();
      throw error;
    }
    listed = new Root(place, runner, lose, this.options.questionMs ?? QUESTION_MS, (ms) => this.backstop(ms));
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

  // *end*, what of *root* is ending, joins whatever of it already was.
  private ends(root: string, end: Promise<void>): Promise<void> {
    const all: Promise<void> = Promise.allSettled([this.endings.get(root), end]).then(() => {
      if (this.endings.get(root) === all) this.endings.delete(root);
    });
    this.endings.set(root, all);
    return end;
  }

  // Everything of *root* ends and it is forgotten, then the mount of *share*, its folder's,
  // goes: the host is letting the folder go, and removes the share next. Its next
  // operation shares its folder and sets it up again.
  teardown(root: string, share: Share): Promise<void> {
    return this.ends(root, (async () => {
      // A setup under way lands first, and what it set up ends with the rest.
      await this.starting.get(root)?.catch(() => {});
      this.registries.delete(root);
      const target = this.roots.get(root);
      if (target) {
        // Out of the list first: the end of its runner is no loss to tell.
        this.roots.delete(root);
        await target.end();
      }
      // What it wrote, written out. A share that does not answer its own flush stalled, and is held too.
      if (!((await this.options.flush?.(share, this.held.has(root))) ?? true)) this.held.add(root);
      await this.options.unmount?.(share);
      // What of it would not end, a process waiting on a share that stalled, still holds the
      // share: the host keeps it in the guest, which could not let it go.
      if (this.held.delete(root)) throw new Error(HELD);
    })());
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
