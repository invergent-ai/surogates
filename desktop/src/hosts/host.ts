// A tool host: one process per root session, holding that folder's sandbox. srt
// keeps its configuration in module globals, so each folder gets its own process
// (spec, Section 1). It starts the file helper inside the sandbox and relays
// file operations to it, and it runs commands itself: it owns the folder's srt and
// is the parent of every command's bwrap. A Node child process with an IPC channel.

import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, type Stats, statSync } from "node:fs";
import type { Server } from "node:net";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import { findOnPath } from "../files/operations.js";
import type { Outcome } from "../link/protocol.js";
import { inside, realpath } from "../files/paths.js";
import { absolutePath, commandEnvironment, makeCaches } from "./environment.js";
import { lockFolder, presentIn, readRecord, removePlaceholders, writeRecord } from "./folder-record.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "./messages.js";
import { HookGuard } from "./hooks.js";
import { GLOB, hideSrtTmp, isReserved, sandboxPolicy } from "./policy.js";
import { CANCELLED, type CommandContext, runCommand } from "./run.js";

const HELPER = fileURLToPath(new URL("../files/helper.js", import.meta.url));
const READY_TIMEOUT_MS = 15_000;
const CREDENTIALS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];
const STOP_COMMANDS_MS = 2_000;

const send = (message: FromHost, then?: () => void) => {
  process.send?.(message, undefined, undefined, then);
};
const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

let helper: ChildProcess | null = null;
let folder: { path: string; dev: number; ino: number } | null = null;
let stopping = false;
let context: CommandContext | null = null;
let guard: HookGuard | null = null;
// Held for the host's life: the kernel lets go of it when the host goes.
let lock: Server | null = null;
let recordPath: string | null = null;
const commands = new Map<string, { controller: AbortController; done: Promise<void> }>();

process.on("message", (raw) => {
  const message = raw as ToHost;
  switch (message.type) {
    case "start":
      if (folder) break;
      start(message).then(
        () => send({ type: "ready" }),
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
      else helper?.stdin?.write(`${JSON.stringify({ id: message.id, kind: message.kind, args: message.args })}\n`);
      break;
    case "cancel":
      commands.get(message.id)?.controller.abort();
      helper?.stdin?.write(`${JSON.stringify({ cancel: message.id })}\n`);
      break;
    case "stop":
      void stop();
      break;
  }
});
process.on("disconnect", () => void stop());
process.on("SIGTERM", () => void stop());

// A path as spelled, and as the file system resolves it. Where it does not exist yet, or cannot be
// read, the part that exists is still resolved: a guard that is not there yet is still where its links lead.
function spellings(path: string): string[] {
  const plain = resolve(path);
  const { path: real } = realpath(plain);
  return real === plain ? [plain] : [plain, real];
}

// What a host killed together with the app left of srt: its bridges (socat, whose
// command line names their socket), then its files.
function sweep(dir: string): void {
  for (const pid of readdirSync("/proc").filter((name) => /^\d+$/.test(name))) {
    try {
      if (readFileSync(`/proc/${pid}/cmdline`, "latin1").includes(`${dir}/`)) process.kill(Number(pid), "SIGKILL");
    } catch {
      // Gone, or not ours to read.
    }
  }
  rmSync(dir, { recursive: true, force: true });
}

// The bound folder is gone, or is not a folder. The app has no folder to check
// before it starts a host (a stat on a stuck mount would freeze it), so the host says.
class FolderUnavailable extends Error {}

async function start(message: HostStart): Promise<void> {
  const home = message.env.HOME;
  if (!home) throw new Error("the app's environment has no HOME");
  if (!isAbsolute(message.tmp)) throw new Error(`the temp folder must be an absolute path: ${message.tmp}`);
  const tmp = resolve(message.tmp);
  const appDirs = message.appDirs.map((dir) => resolve(dir));
  let path: string;
  let stats: Stats;
  try {
    path = realpathSync(message.folder);
    stats = statSync(path);
  } catch {
    throw new FolderUnavailable(`the folder ${message.folder} is not there`);
  }
  if (!stats.isDirectory()) throw new FolderUnavailable(`the folder ${message.folder} is not a folder`);
  const globbed = [path, tmp, ...appDirs].find((entry) => GLOB.test(entry));
  if (globbed) throw new Error(`this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${globbed}`);
  if (isReserved(path)) {
    throw new Error(`the folder ${path} is inside one of this computer's system folders`);
  }
  // A sandbox whose writable folder held the home folder, the app's own data or
  // files (which run outside the sandbox) or a credential folder would hand all
  // of it to the agent. Each is compared as spelled and as resolved: a link
  // would otherwise walk around the check.
  const homes = spellings(home);
  const guarded = [message.dataDir, ...appDirs, ...homes.flatMap((dir) => CREDENTIALS.map((name) => join(dir, name)))]
    .flatMap(spellings);
  const refused = [...new Set([resolve(message.folder), path])].some(
    (candidate) => candidate === "/" || homes.some((dir) => inside(dir, candidate)) ||
      guarded.some((dir) => inside(dir, candidate) || inside(candidate, dir)),
  );
  if (refused) throw new Error(`the folder ${path} holds this computer's home folder or the app's own data`);
  folder = { path, dev: stats.dev, ino: stats.ino };
  // One host per folder. Then, if the host before this one was killed, what srt
  // left over the names that were absent when it started.
  const key = `${stats.dev}-${stats.ino}`;
  // srt's own temp files (its bridges' sockets, the empty folders it mounts) go
  // through os.tmpdir(), read at each call: here, in a folder only this folder's
  // host uses, so whatever a killed host left there is provably its own.
  const srtTmp = join(message.dataDir, "srt", key);
  // A unix socket's path holds at most 107 bytes; srt's longest name here is claude-socks-<16 hex>.sock.
  if (Buffer.byteLength(join(srtTmp, `claude-socks-${"0".repeat(16)}.sock`)) > 107) {
    throw new Error(`the app's data folder's path is too long for the sandbox's sockets: ${srtTmp}`);
  }
  lock = await lockFolder(stats.dev, stats.ino);
  // Before anything in the folder is touched: a host killed with the app may have
  // left its commands' sandboxes and bridges running, and each names this folder.
  sweep(srtTmp);
  mkdirSync(srtTmp, { recursive: true, mode: 0o700 });
  process.env.TMPDIR = srtTmp;
  const record = join(message.dataDir, "folders", `${key}.json`);
  const last = readRecord(record);
  const killed = last?.state === "running" ? last : null;
  if (killed) removePlaceholders(path, killed.present);
  const present = presentIn(path);
  const inherited = killed?.hooks ? new Map(Object.entries(killed.hooks)) : null;
  const running = (hooks: ReadonlyMap<string, string> | null) =>
    writeRecord(record, { state: "running", present, hooks: hooks ? Object.fromEntries(hooks) : null });
  // On disk before srt puts anything in the folder, with a killed host's baseline
  // kept: commands wait for the guard, which records its own once it knows it.
  running(inherited);
  recordPath = record;
  // Its first look finds the user's own hooks, while srt starts. After a killed
  // host, that host's are the user's, and the look catches what its commands left.
  // Commands can write the folder and the session's temp folder: a hook linked into either is theirs.
  guard = new HookGuard(path, { inherited, known: running, writable: [path, ...spellings(tmp)] });
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
  const policy = sandboxPolicy({ folder: path, tmp, home, appDirs, bwrapPath, socatPath, rgPath });
  await SandboxManager.initialize(policy);
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
      // A helper that dies takes its host with it, srt cleaned up: the app answers
      // whatever was running as interrupted and starts a new host for the next operation.
      else if (!stopping) void stop(1);
    });
  });
  // The helper's sandbox lasts as long as the host. Left in srt's count, it
  // would keep srt from ever removing a command's placeholders.
  SandboxManager.cleanupAfterCommand();
  context = { folder: path, home, env, claudeWasAbsent: !existsSync(join(path, ".claude")) };
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
    const refused = await hooks.refusal();
    // Stopped or cancelled while the folder was looked through: it never starts.
    const outcome = refused ?? (stopping || controller.signal.aborted
      ? CANCELLED
      : await hooks.after(await runCommand(args, ready, controller.signal, id)));
    commands.delete(id);
    send({ type: "result", id, outcome });
  })();
  commands.set(id, { controller, done });
}

async function stop(code = 0): Promise<void> {
  if (stopping) return;
  stopping = true;
  const running = [...commands.values()];
  for (const { controller } of running) controller.abort();
  await Promise.race([
    Promise.all(running.map(({ done }) => done)),
    new Promise((resolve) => setTimeout(resolve, STOP_COMMANDS_MS)),
  ]);
  helper?.kill("SIGKILL");
  // What a stopped command left, before the record can say the host stopped
  // cleanly. A look that could not see the whole folder, or a host killed
  // during it, leaves "running" and the baseline for the next host.
  const clean = (await guard?.settle()) ?? true;
  await leave(code, clean);
}

// The way out: srt removes its sockets and placeholders, then the record says the
// host stopped cleanly, if it did, so the next one has nothing to clear.
async function leave(code: number, clean: boolean): Promise<void> {
  await SandboxManager.reset().catch(() => {});
  try {
    if (recordPath && clean) writeRecord(recordPath, { state: "stopped", present: [], hooks: null });
  } finally {
    lock?.close();
    process.exit(code);
  }
}
