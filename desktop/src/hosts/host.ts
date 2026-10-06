// A tool host: one process per root session, holding that folder's sandbox. srt
// keeps its configuration in module globals, so each folder gets its own process
// (spec, Section 1). It starts the file helper inside the sandbox and relays
// file operations to it, and it runs commands itself: it owns the folder's srt and
// is the parent of every command's bwrap. srt asks it about every destination off
// the allowed list: it refuses this computer's own, and asks the app about the
// rest. A Node child process with an IPC channel.

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { isIP, type Server } from "node:net";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { type NetworkHostPattern, SandboxManager } from "@anthropic-ai/sandbox-runtime";

import { BOOT_ID, checkFolder, spellings } from "../binding/folder.js";
import { findOnPath } from "../files/operations.js";
import type { Outcome } from "../link/protocol.js";
import { inside, realpath } from "../files/paths.js";
import { absolutePath, commandEnvironment, makeCaches } from "./environment.js";
import { type FolderRecord, lockFolder, presentIn, readRecord, removePlaceholders, writeRecord } from "./folder-record.js";
import { type Destination, FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "./messages.js";
import { HookGuard } from "./hooks.js";
import { destination, GLOB, hideSrtTmp, quote, reach, sandboxPolicy } from "./policy.js";
import { appeared, extraDenies, GRANT_CHANGED, identity, protectedKeys, srtTargets } from "./restarts.js";
import { CANCELLED, unenterable, workdir } from "../guest/command.js";
import { Processes } from "../guest/processes.js";
import type { SessionRunner } from "../guest/runner-process.js";
import { type CommandContext, runCommand } from "./run.js";
import { startRunner, stopRunner } from "./session-runner.js";

const HELPER = fileURLToPath(new URL("../files/helper.js", import.meta.url));
const READY_TIMEOUT_MS = 15_000;
const STOP_COMMANDS_MS = 2_000;
// While background processes or the runner live, the hook guard looks this often: one may write a hook between commands.
const WATCH_MS = 5_000;
// A protected key that comes and goes restarts the runner at most once in this long; a restart asked for sooner waits.
const RESTART_WINDOW_MS = 10_000;
// How many destinations of a kind a notice names.
const NOTICE_NAMES = 20;
const PROCESS_KINDS = new Set(["start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"]);

// A channel the app has closed is not an error: with no callback, Node would raise
// one on process and end the host before its final look. Every exit goes through stop.
const send = (message: FromHost, then: (error: Error | null) => void = () => {}) => {
  if (process.connected) process.send?.(message, undefined, undefined, then);
  else then(new Error("the app's channel is closed"));
};

let helper: ChildProcess | null = null;
let folder: { path: string; dev: number; ino: number } | null = null;
let stopping = false;
// Stopped because something failed: the commands it stops go unanswered, and the
// app answers them as interrupted, with the warning to check what they did.
let failing = false;
let context: CommandContext | null = null;
let guard: HookGuard | null = null;
// Held for the host's life: the kernel lets go of it when the host goes.
let lock: Server | null = null;
let recordPath: string | null = null;
// What the folder's record holds, while this host runs.
let saved: FolderRecord | null = null;
const commands = new Map<string, { controller: AbortController; done: Promise<void> }>();
let processes: Processes | null = null;
// The root's session runner: starting from its first background process, then up
// until it stops, dies or is restarted. Until it is up, commands get a sandbox of their own.
let runner: Promise<SessionRunner> | null = null;
let liveRunner: SessionRunner | null = null;
let watching: NodeJS.Timeout | null = null;
// Once a command of this root's has run in the guest: what it left running there, or
// a cancelled one still ending, can write a hook at any time, so the look every
// WATCH_MS goes on until the host stops.
let guestCommands = false;
// The live runner's protected keys, from a walk that started once it was up (performance.now()),
// and each path its wrap denies writes to, as the wrap held it (restarts.ts identity).
type Baseline = { keys: ReadonlySet<string>; since: number; targets: ReadonlyMap<string, string | null> };
let baseline: Baseline | null = null;
// While the runner restarts, run and start wait for the new one.
let restarting: Promise<void> | null = null;
// A restart's stop of its old runner, which SIGKILL bounds.
let retiring: Promise<void> = Promise.resolve();
let lastRestart = -Infinity;
let deferred: NodeJS.Timeout | null = null;
// The latest reason asked for while a restart is deferred: the one the agent is told.
let deferredReason = "";
// Runs in flight in the runner: a look between commands does not restart it under them.
let runnerRuns = 0;
// Network asks the app has not answered, by id, and the decision open for each
// destination ("host:port"): a connection to a destination already being decided waits for it.
// told: a command's output has said that it waits.
const asks = new Map<number, { host: string; key: string; answer: (allow: boolean) => void; told: boolean }>();
const asking = new Map<string, Promise<boolean>>();
let lastAsk = 0;
// Destinations refused since a command last answered, as this computer's own, as not
// allowed, or as not looked up: the agent is told once.
const own = new Set<string>();
const refused = new Set<string>();
const unknown = new Set<string>();

process.on("message", (raw) => {
  const message = raw as ToHost;
  switch (message.type) {
    case "start":
      if (folder) break;
      start(message).then(
        () => send({ type: "ready", processes: processes?.handles() ?? [] }),
        (error: unknown) => send(
          {
            type: "failed",
            message: error instanceof Error ? error.message : String(error),
            ...(error instanceof FolderUnavailable ? { folder: true as const } : {}),
          },
          () => void stop(1),
        ),
      );
      break;
    case "op":
      if (!sameFolder()) send({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
      else if (message.kind === "run") command(message.id, message.args);
      else if (PROCESS_KINDS.has(message.kind)) processOp(message.id, message.kind, message.args);
      else helper?.stdin?.write(`${JSON.stringify({ id: message.id, kind: message.kind, args: message.args })}\n`);
      break;
    case "cancel":
      commands.get(message.id)?.controller.abort();
      helper?.stdin?.write(`${JSON.stringify({ cancel: message.id })}\n`);
      break;
    case "refusal":
      // A command for a folder replaced since the start would run on the replacement.
      if (!sameFolder()) send({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
      else void guard?.refusal().then((refused) => !failing && send({ type: "result", id: message.id, outcome: refused ?? { ok: null } }));
      break;
    case "after":
      guestCommands = true;
      void guard?.after(message.outcome).then((outcome) => {
        watchHooks();
        if (!failing) send({ type: "result", id: message.id, outcome });
      });
      break;
    case "handles":
      // What the guest's processes can write, they write at any time: the look every WATCH_MS goes on, as after a command.
      guestCommands = true;
      try {
        save({ processes: message.handles });
      } catch {
        // Kept from the last write.
      }
      watchHooks();
      break;
    case "restart":
      restart(GRANT_CHANGED);
      break;
    case "answer":
      answered(message.id, message.allow, message.remember);
      break;
    case "stop":
      void stop();
      break;
  }
});
process.on("disconnect", () => void stop());
process.on("SIGTERM", () => void stop());

// What a host killed together with the app left of srt: its bridges (socat, whose
// command line names their socket), then its files.
function sweep(dir: string): void {
  for (const pid of readdirSync("/proc").filter((name) => /^\d+$/.test(name))) {
    try {
      // The user's own only: reading a command line waits on that process's memory, which one stuck in the kernel holds.
      if (statSync(`/proc/${pid}`).uid !== process.getuid?.()) continue;
      if (readFileSync(`/proc/${pid}/cmdline`, "latin1").includes(`${dir}/`)) process.kill(Number(pid), "SIGKILL");
    } catch {
      // Gone, or not ours to read.
    }
  }
  rmSync(dir, { recursive: true, force: true });
}

// The bound folder is gone, is not a folder, or was replaced. The app has no folder to check
// before it starts a host (a stat on a stuck mount would freeze it), so the host says.
class FolderUnavailable extends Error {}

async function start(message: HostStart): Promise<void> {
  const home = message.env.HOME;
  if (!home) throw new Error("the app's environment has no HOME");
  if (!isAbsolute(message.tmp)) throw new Error(`the temp folder must be an absolute path: ${message.tmp}`);
  const tmp = resolve(message.tmp);
  const appDirs = message.appDirs.map((dir) => resolve(dir));
  const checked = checkFolder(message.folder, { home, dataDir: message.dataDir, appDirs });
  if (!checked.ok) throw checked.missing ? new FolderUnavailable(checked.message) : new Error(checked.message);
  const globbed = [tmp, ...appDirs].find((entry) => GLOB.test(entry));
  if (globbed) throw new Error(`this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${globbed}`);
  const { path, dev, ino } = checked;
  // A reboot can renumber the folder's mount: after one, only the inode is compared.
  // A boot id that could not be read counts as this boot.
  const { expect } = message;
  const rebooted = expect.boot !== "" && BOOT_ID !== "" && expect.boot !== BOOT_ID;
  if (expect.ino !== ino || (!rebooted && expect.dev !== dev)) {
    throw new FolderUnavailable(`the folder ${message.folder} was replaced after it was confirmed for this chat`);
  }
  folder = { path, dev, ino };
  // One host per folder. Then, if the host before this one was killed, what srt
  // left over the names that were absent when it started.
  const key = `${dev}-${ino}`;
  // srt's own temp files (its bridges' sockets, the empty folders it mounts) go
  // through os.tmpdir(), read at each call: here, in a folder only this folder's
  // host uses, so whatever a killed host left there is provably its own.
  const srtTmp = join(message.dataDir, "srt", key);
  // A unix socket's path holds at most 107 bytes; srt's longest name here is claude-socks-<16 hex>.sock.
  if (Buffer.byteLength(join(srtTmp, `claude-socks-${"0".repeat(16)}.sock`)) > 107) {
    throw new Error(`the app's data folder's path is too long for the sandbox's sockets: ${srtTmp}`);
  }
  lock = await lockFolder(dev, ino);
  // Before anything in the folder is touched: the bridges (socat) a host killed with
  // the app left running, and anything else whose command line names srt's folder.
  sweep(srtTmp);
  mkdirSync(srtTmp, { recursive: true, mode: 0o700 });
  process.env.TMPDIR = srtTmp;
  const record = join(message.dataDir, "folders", `${key}.json`);
  const last = readRecord(record);
  const killed = last?.state === "running" ? last : null;
  if (killed) removePlaceholders(path, killed.present);
  const present = presentIn(path);
  const inherited = killed?.hooks ? new Map(Object.entries(killed.hooks)) : null;
  // The processes the last host started: those still running ended with it, and those that ended by
  // themselves keep their real exit code. The registry answers for them until the cloud would forget them.
  const ended = last?.processes ?? [];
  // On disk before srt puts anything in the folder, with a killed host's baseline
  // kept: commands wait for the guard, which records its own once it knows it.
  saved = { state: "running", present, hooks: killed?.hooks ?? null, processes: ended };
  writeRecord(record, saved);
  recordPath = record;
  const running = (hooks: ReadonlyMap<string, string>) => save({ hooks: Object.fromEntries(hooks) });
  // Its first look finds the user's own hooks, while srt starts. After a killed
  // host, that host's are the user's, and the look catches what its commands left.
  // Commands can write the folder and the session's temp folder: a hook linked into either is theirs.
  guard = new HookGuard(path, { inherited, known: running, writable: [path, ...spellings(tmp)], seen });
  mkdirSync(tmp, { recursive: true });
  makeCaches(tmp);
  const env = commandEnvironment(message.env, tmp);
  // srt sets the sandbox's TMPDIR from this; its default is shared by every sandbox.
  process.env.CLAUDE_CODE_TMPDIR = tmp;
  // srt and the shell it wraps a command in run outside the sandbox, with the
  // folder as their working folder, and srt itself looks up which, rg and the
  // shell through this process's PATH. Only absolute entries outside the folder
  // and the temp folder are kept, and srt's own tools go by absolute path: no
  // program a command wrote can run out here.
  const hostPath = absolutePath(process.env.PATH ?? "").split(":")
    .filter((entry) => entry && ![path, tmp].some((dir) => inside(realpath(entry).path, dir)))
    .join(":") || "/usr/bin:/bin";
  process.env.PATH = hostPath;
  // A user's rg config could hide nested paths from srt's scan, as it could from the helper's searches.
  delete process.env.RIPGREP_CONFIG_PATH;
  const bwrapPath = message.bwrapPath ?? findOnPath("bwrap", hostPath, "/") ?? undefined;
  const socatPath = findOnPath("socat", hostPath, "/") ?? undefined;
  const rgPath = findOnPath("rg", hostPath, "/") ?? undefined;
  // An address granted on one network can be this computer's own on another, and srt
  // never judges a listed literal again: such a grant is left out while it is.
  const domains: string[] = [];
  for (const domain of message.domains) {
    if (isIP(domain.replace(/^\[(.*)\]$/, "$1")) && (await reach(domain).catch(() => "own")) === "own") continue;
    domains.push(domain);
  }
  const policy = sandboxPolicy({ folder: path, tmp, home, appDirs, bwrapPath, socatPath, rgPath, domains });
  await SandboxManager.initialize(policy, askApp);
  // A warning names a protection that is missing, such as seccomp's unix-socket filter: fail closed.
  const { errors, warnings } = SandboxManager.checkDependencies();
  if (errors.length || warnings.length) throw new Error([...errors, ...warnings].join("; "));
  // srt's proxy bridges listen in its temp folder, which the policy hides.
  const sockets = [SandboxManager.getLinuxHttpSocketPath(), SandboxManager.getLinuxSocksSocketPath()]
    .filter((socket): socket is string => Boolean(socket));
  SandboxManager.updateConfig({
    ...policy, filesystem: { ...policy.filesystem, allowRead: [...(policy.filesystem.allowRead ?? []), ...sockets] },
  });
  // srt mounts placeholders over its protected names in the working directory at
  // wrap time. The helper guards those names itself, so it is wrapped from the
  // temp folder and the user's folder stays as it was.
  process.chdir(tmp);
  const { argv } = await SandboxManager.wrapWithSandboxArgv(`${quote(process.execPath)} ${quote(HELPER)}`);
  // Every later wrap is a command's, and gets srt's protected names in the
  // folder itself. Once, here: a wrap awaits, so a chdir per wrap would race.
  process.chdir(path);
  const [file, flag, line] = argv;
  if (!file || flag === undefined || line === undefined) throw new Error("srt returned no command");
  // The app-built environment only: srt's returned env is this process's own.
  // --norc --noprofile: with a socket for stdin, as this pipe is, bash reads ~/.bashrc out here.
  const child = spawn(file, ["--norc", "--noprofile", flag, hideSrtTmp(line)], {
    cwd: path,
    env: { ...env, SUROGATE_FOLDER: path, ELECTRON_RUN_AS_NODE: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  helper = child;
  // A write to a helper that has died is not an error of its own: its exit is the one way out.
  child.stdin.on("error", () => {});
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = (stderr + chunk.toString()).slice(-4000);
  });
  await new Promise<void>((resolve, reject) => {
    let up = false;
    const timer = setTimeout(() => reject(new Error(`the file helper did not start: ${stderr}`)), READY_TIMEOUT_MS);
    createInterface({ input: child.stdout }).on("line", (line) => {
      if (!up) {
        up = true;
        clearTimeout(timer);
        if (line === '{"ready":true}') resolve();
        else reject(new Error(`the file helper said ${line}: ${stderr}`));
        return;
      }
      try {
        const reply = JSON.parse(line) as { id: string; outcome: Outcome };
        send({ type: "result", id: reply.id, outcome: reply.outcome });
      } catch {
        // Not a reply; the helper writes nothing else.
      }
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", () => {
      clearTimeout(timer);
      if (!up) reject(new Error(`the file helper exited: ${stderr}`));
      // A helper that dies takes its host with it, srt cleaned up and its commands
      // stopped unanswered: the app answers them as interrupted and starts a new host
      // for the next operation.
      else if (!stopping) void stop(1);
    });
  });
  // The helper's sandbox lasts as long as the host. Left in srt's count, it
  // would keep srt from ever removing a command's placeholders.
  SandboxManager.cleanupAfterCommand();
  context = { folder: path, home, env, claudeWasAbsent: !existsSync(join(path, ".claude")) };
  const hooks = guard;
  const ready = context;
  processes = new Processes({
    place: async (requested) => {
      const cwd = workdir(ready, requested);
      return { cwd, unenterable: unenterable(cwd) };
    },
    runner: sessionRunner,
    refusal: () => hooks.refusal(),
    ended,
    // The handle is only for answering after the app quit: a record that cannot be written does not stop the process.
    // Once the host stops, what it ends ended when the app quit, as the record already says of it.
    save: (handles) => {
      if (stopping) return;
      try {
        save({ processes: handles });
      } catch {
        // Kept from the last write.
      }
    },
    live: (count) => {
      send({ type: "processes", live: count });
      watchHooks();
    },
  });
  // The registry's 30-minute filter is the one: what it dropped leaves the record too.
  save({ processes: processes.handles() });
}

// srt's ask callback, for a connection to a destination off the allowed list. Every
// connection to a destination already being decided waits for that decision, so one
// npm install asks once. Fails closed: a destination srt would not dial, a host
// that is stopping, or no app to ask refuses the connection.
function askApp({ host, port }: NetworkHostPattern): Promise<boolean> {
  const found = destination(host, port);
  if (!found || stopping) return Promise.resolve(false);
  const key = `${found.host}:${found.port}`;
  const open = asking.get(key);
  if (open) return open;
  const decided = decide(found, key);
  asking.set(key, decided);
  void decided.then(() => {
    if (asking.get(key) === decided) asking.delete(key);
  });
  return decided;
}

// This computer's own services are refused before anyone is asked, and so is a name
// that cannot be looked up; the app asks its user about the rest, saying when it is
// on a private network.
async function decide(found: Destination, key: string): Promise<boolean> {
  // A lookup or an interface read that throws refuses, as a name that cannot be looked up does.
  const where = await reach(found.host).catch(() => null);
  if (where === "own") own.add(key);
  if (where === null) unknown.add(key);
  if ((where !== "public" && where !== "private") || stopping) return false;
  lastAsk += 1;
  const id = lastAsk;
  const answer = new Promise<boolean>((resolve) => asks.set(id, { host: found.host, key, answer: resolve, told: false }));
  send({ type: "ask", id, ...found, privateNetwork: where === "private" }, (error) => {
    if (error) answered(id, false, false);
  });
  return answer;
}

// The app's answer to a network ask, for every connection waiting on it. Remembered,
// the host goes through from now on, on every port: srt reads its list at each
// connection, the session runner's included. An answer for no open ask changes nothing.
function answered(id: number, allow: boolean, remember: boolean): void {
  const ask = asks.get(id);
  if (!ask) return;
  asks.delete(id);
  const config = SandboxManager.getConfig();
  if (allow && remember && config) {
    const { allowedDomains } = config.network;
    try {
      SandboxManager.updateConfig({ ...config, network: { ...config.network, allowedDomains: [...allowedDomains, ask.host] } });
    } catch {
      // Let through this once all the same: the user allowed it.
    }
  }
  if (!allow) refused.add(ask.key);
  ask.answer(allow);
}

// A look every WATCH_MS while any background process or the runner is alive, and one
// more after the last one ends: it may have written a hook on its way out. An idle
// runner is looked at too: a protected path made outside the app restarts it before
// the next command. Decided again after the look: a process started during it found
// a look already set and armed none.
function watchHooks(): void {
  if (watching || !guard || stopping) return;
  const hooks = guard;
  const alive = () => (processes?.live ?? 0) > 0 || liveRunner !== null || guestCommands;
  watching = setTimeout(() => void (async () => {
    // A folder replaced since the start is not this chat's: no look or runner goes over it.
    if (!sameFolder()) return void stop(1);
    const was = alive();
    await hooks.watch();
    watching = null;
    if (was || alive()) watchHooks();
  })(), WATCH_MS);
}

// Started at the root's first background process. A runner that dies unexpectedly
// ends its processes (the registry notes why); the next start starts another, and
// commands get sandboxes of their own until then. A restart starts the next one itself;
// a start cancelled while it waited starts none.
async function sessionRunner(signal: AbortSignal): Promise<SessionRunner> {
  await restarted(signal);
  if (signal.aborted) throw new Error("the start was cancelled");
  return launch();
}

function launch(): Promise<SessionRunner> {
  if (!context || stopping) return Promise.reject(new Error("the tool host is stopping"));
  if (runner) return runner;
  // Each clears only itself: a runner that went may answer after the next one started.
  let up: SessionRunner | null = null;
  const starting: Promise<SessionRunner> = openRunner(context, () => {
    if (runner === starting) runner = null;
    if (liveRunner === up) {
      liveRunner = null;
      baseline = null;
      // The next runner's wrap covers whatever a restart waited for.
      if (deferred) clearTimeout(deferred);
      deferred = null;
    }
  }).then(
    ({ started, keys }) => {
      up = started;
      // A runner lost during its baseline's walk is not live, and sets no baseline.
      if (runner === starting) {
        liveRunner = started;
        baseline = keys;
      }
      watchHooks();
      return started;
    },
    (error: unknown) => {
      if (runner === starting) runner = null;
      throw error;
    },
  );
  runner = starting;
  return starting;
}

// srt's own denies miss protected paths: the runner's wrap denies writes to every
// one the host's walk finds that they do not cover (restarts.ts).
// Its baseline is what the folder holds once it is up: srt's placeholders are there by then.
async function openRunner(ready: CommandContext, onLost: () => void): Promise<{ started: SessionRunner; keys: Baseline }> {
  const literals = extraDenies(ready.folder, await protectedKeys(ready.folder));
  // Each as it is just before the wrap. srt would leave a placeholder for one gone
  // since the walk: it is left out, and its return restarts the runner.
  const targets = new Map(literals.map((path) => [path, identity(path)]));
  const denyWrite = literals.filter((path) => targets.get(path) !== null);
  const started = await startRunner(ready, onLost, { denyWrite, allowWrite: [] });
  const since = performance.now();
  for (const path of srtTargets(ready.folder)) targets.set(path, identity(path));
  try {
    return { started, keys: { keys: await protectedKeys(ready.folder), since, targets } };
  } catch (error) {
    await started.stop();
    throw error;
  }
}

// What a look found, against the live runner's baseline: a protected key that was
// not there when the runner was wrapped is writable inside it, unless it lies under a
// path the wrap denies; so is a denied path replaced or made since. Either restarts it.
// A look that started before the baseline's walk may have seen the old runner's folder.
// While a restart waits for its window, a key seen again adds nothing: that restart takes a new baseline.
// A look between commands leaves runs in flight to finish: the first of them to end
// decides with its own look, and a restart then cuts the others. Until then background
// processes can write the new path, as a command in a sandbox of its own can.
function seen(keys: ReadonlySet<string>, startedAt: number, between: boolean): void {
  const known = baseline;
  if (!liveRunner || deferred || !folder || !known || startedAt < known.since || (between && runnerRuns > 0)) return;
  const root = folder.path;
  const covered = (key: string) => {
    for (let at = key; at.length > root.length; at = dirname(at)) if (known.targets.has(at)) return true;
    return false;
  };
  const added = [...keys].filter((key) => !known.keys.has(key) && !covered(key));
  const changed = [...known.targets].filter(([path, was]) => identity(path) !== was).map(([path]) => path);
  const first = [...added, ...changed].sort()[0];
  if (first) restart(appeared(relative(root, first)));
}

// A new runner, wrapped from the folder as it is now: srt then sees a new .git as a
// folder and covers its hooks and config. Its live processes end, released from srt's
// count once, with its bwrap; work that comes meanwhile waits for the new one.
function restart(reason: string): void {
  if (stopping || !(liveRunner || restarting)) return;
  // A new runner would be wrapped over the replacement, and leave srt's placeholders in it.
  if (!sameFolder()) return void stop(1);
  if (deferred) {
    deferredReason = reason;
    return;
  }
  const wait = lastRestart + RESTART_WINDOW_MS - performance.now();
  // Deferred, not dropped. One asked for during a restart comes after it: that wrap may predate the reason.
  if (restarting || wait > 0) {
    deferredReason = reason;
    // Still deferred while it waits for a restart under way: a grant then only updates the reason.
    const timer = setTimeout(() => void (restarting ?? Promise.resolve()).then(() => {
      // Dropped meanwhile, with the runner it was for.
      if (deferred !== timer) return;
      deferred = null;
      // A key's restart leaves runs in flight to finish, as a timed look's does: the first to end decides with its own look.
      if (deferredReason === GRANT_CHANGED || runnerRuns === 0) restart(deferredReason);
    }), Math.max(wait, 0));
    deferred = timer;
    return;
  }
  const old = liveRunner;
  if (!old) return;
  lastRestart = performance.now();
  runner = null;
  liveRunner = null;
  baseline = null;
  processes?.restart(reason);
  retiring = old.stop();
  restarting = (async () => {
    await retiring;
    // One that cannot start leaves none: the next start tries again, and commands get sandboxes of their own.
    await launch().catch(() => {});
  })().finally(() => {
    restarting = null;
  });
}

// Until a restart is done, or the run is cancelled.
async function restarted(signal: AbortSignal): Promise<void> {
  const cancelled = new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
  while (restarting && !signal.aborted) await Promise.race([restarting, cancelled]);
}

// The destinations refused since the last notice and those still waiting for the app,
// then a restart's notice, after any hooks notice, in the next run that answers ok.
// srt does not say which command asked, so a command that ends first may carry another's.
function withNotice(outcome: Outcome): Outcome {
  if (!("ok" in outcome)) return outcome;
  const local = line(own, (names) => `This computer does not let a chat reach its own network services (${names})`);
  // Neutral: a denial can also be a prompt that failed or was dismissed, or no one to ask.
  const denied = line(refused, (names) => `This computer did not allow network access to ${names}.`);
  // Each open ask once: the agent learns that it waits, then, once answered, that it was refused.
  const untold = [...asks.values()].filter((ask) => !ask.told);
  for (const ask of untold) ask.told = true;
  const waits = line(untold.map((ask) => ask.key), (names) => `Still waiting for this computer's user to allow network access to ${names}.`);
  const lost = line(unknown, (names) => `This computer could not look up ${names}.`);
  own.clear();
  refused.clear();
  unknown.clear();
  const notice = [local, denied, waits, lost, processes?.takeNotice()].filter(Boolean).join("\n");
  if (!notice) return outcome;
  const ok = outcome.ok as { output: string; returncode: number; timed_out: boolean };
  return { ok: { ...ok, output: `${ok.output}${ok.output ? "\n" : ""}${notice}` } };
}

// A notice's line about *keys*, naming the first NOTICE_NAMES of them and then how many
// more, so a sweep of ports cannot flood the agent's output; none for no keys.
function line(keys: Iterable<string>, say: (names: string) => string): string | null {
  const all = [...keys];
  if (all.length === 0) return null;
  const more = all.length > NOTICE_NAMES ? ` and ${all.length - NOTICE_NAMES} more` : "";
  return say(`${all.slice(0, NOTICE_NAMES).join(", ")}${more}`);
}

function save(change: Partial<FolderRecord>): void {
  if (!recordPath || !saved) return;
  saved = { ...saved, ...change };
  writeRecord(recordPath, saved);
}

function sameFolder(): boolean {
  if (!folder) return false;
  try {
    const { dev, ino } = statSync(folder.path);
    return dev === folder.dev && ino === folder.ino;
  } catch {
    return false;
  }
}

// A command runs here, not in the helper: the host owns the folder's srt and
// must be the parent of its bwrap. The folder is looked through before it, when
// the last look could not see all of it, and after it, for the hooks it left.
function command(id: string, args: Record<string, unknown>): void {
  if (!context || !guard || stopping || commands.has(id)) return;
  const ready = context;
  const hooks = guard;
  const controller = new AbortController();
  const done = (async () => {
    const outcome = await commandOutcome(args, ready, hooks, controller.signal, id);
    commands.delete(id);
    if (!failing) send({ type: "result", id, outcome });
  })();
  commands.set(id, { controller, done });
}

// A run that comes during a restart waits for the new runner, then asks the guard: a
// block raised meanwhile stops it. A restart the guard's own look starts is waited for
// too: the run would otherwise get a sandbox of its own, without the runner's denies.
// Stopped or cancelled while it waited, or while the folder was looked through: it never starts.
async function commandOutcome(
  args: Record<string, unknown>, ready: CommandContext, hooks: HookGuard, signal: AbortSignal, id: string,
): Promise<Outcome> {
  do {
    await restarted(signal);
    if (stopping || signal.aborted) return CANCELLED;
    const refused = await hooks.refusal();
    if (refused) return refused;
  } while (restarting);
  if (stopping || signal.aborted) return CANCELLED;
  const inRunner = liveRunner;
  if (inRunner) runnerRuns += 1;
  const outcome = await runCommand(args, ready, signal, id, inRunner);
  if (inRunner) runnerRuns -= 1;
  return withNotice(await hooks.after(outcome));
}

// The background process kinds. A wait can last minutes: a cancel or a stop ends it.
function processOp(id: string, kind: string, args: Record<string, unknown>): void {
  if (!processes || stopping || commands.has(id)) return;
  const registry = processes;
  const controller = new AbortController();
  const done = (async () => {
    const outcome = await registry.answer(kind, args, controller.signal);
    commands.delete(id);
    if (!failing) send({ type: "result", id, outcome });
  })();
  commands.set(id, { controller, done });
}

async function stop(code = 0): Promise<void> {
  if (stopping) return;
  stopping = true;
  failing = code !== 0;
  if (watching) clearTimeout(watching);
  if (deferred) clearTimeout(deferred);
  deferred = null;
  const running = [...commands.values()];
  for (const { controller } of running) controller.abort();
  await Promise.race([
    Promise.all(running.map(({ done }) => done)),
    new Promise((resolve) => setTimeout(resolve, STOP_COMMANDS_MS)),
  ]);
  helper?.kill("SIGKILL");
  // A restart's old runner first: its own SIGKILL bounds its stop. A new one then
  // starts no more, or is the runner to stop, its launch bounded as a first one's is.
  await retiring;
  // Its background processes go with its sandbox, before the last look.
  await stopRunner(runner, liveRunner, STOP_COMMANDS_MS);
  // What a stopped command left, before the record can say the host stopped
  // cleanly. A look that could not see the whole folder, or a host killed
  // during it, leaves "running" and the baseline for the next host. So does a folder
  // moved or replaced: the look would change the hooks of a folder that is not this chat's.
  const clean = sameFolder() && ((await guard?.settle()) ?? true);
  await leave(code, clean);
}

// The way out: srt removes its sockets and placeholders, then the record says the
// host stopped cleanly, if it did, so the next one has nothing to clear.
async function leave(code: number, clean: boolean): Promise<void> {
  await SandboxManager.reset().catch(() => {});
  try {
    if (recordPath && clean) writeRecord(recordPath, { state: "stopped", present: [], hooks: null, processes: saved?.processes ?? [] });
  } finally {
    lock?.close();
    process.exit(code);
  }
}
