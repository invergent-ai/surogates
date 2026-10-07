// A tool host: one process per root session, holding that folder's file helper in srt.
// srt keeps its configuration in module globals, so each folder gets its own process
// (spec, Section 1). It starts the file helper inside the sandbox and relays the file
// operations to it. Commands run in the VM (spec, Section 11): around each, the host is
// the hook guard, and it keeps the folder's record, the user's hooks and the handles of
// the root's processes in the guest. A Node child process with an IPC channel.

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { isIP, type Server } from "node:net";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import { BOOT_ID, checkFolder } from "../binding/folder.js";
import { findOnPath } from "../files/operations.js";
import type { Outcome } from "../link/protocol.js";
import { inside, realpath } from "../files/paths.js";
import { APP_QUIT, FINISHED_TTL_SECONDS, lostWith } from "../guest/processes.js";
import { reach } from "../vm/egress.js";
import { absolutePath, commandEnvironment, makeCaches } from "./environment.js";
import { type FolderRecord, lockFolder, readRecord, writeRecord } from "./folder-record.js";
import { HookGuard } from "./hooks.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "./messages.js";
import { GLOB, hideSrtTmp, quote, sandboxPolicy } from "./policy.js";

const HELPER = fileURLToPath(new URL("../files/helper.js", import.meta.url));
const READY_TIMEOUT_MS = 15_000;
// Once a command of the root's may have run in the guest, the hook guard looks this often until
// the host stops: what it left running there, or a cancelled one still ending, can write a hook at any time.
const WATCH_MS = 5_000;

// A channel the app has closed is not an error: with no callback, Node would raise
// one on process and end the host before its final look. Every exit goes through stop.
const send = (message: FromHost, then: (error: Error | null) => void = () => {}) => {
  if (process.connected) process.send?.(message, undefined, undefined, then);
  else then(new Error("the app's channel is closed"));
};

let helper: ChildProcess | null = null;
let folder: { path: string; dev: number; ino: number } | null = null;
let stopping = false;
// Stopped because something failed: what it was asked goes unanswered, and the app
// answers it as interrupted, with the warning to check what it did.
let failing = false;
let guard: HookGuard | null = null;
// Held for the host's life: the kernel lets go of it when the host goes.
let lock: Server | null = null;
let recordPath: string | null = null;
// What the folder's record holds, while this host runs.
let saved: FolderRecord | null = null;
let watching: NodeJS.Timeout | null = null;
// The root's background processes alive in the guest, as the guest last said, and until when a guest
// run answered before its processes went (cancelled, timed out) may still be ending. Its kill is sent
// as it is answered.
// ponytail: a fixed grace for that kill; the guest saying the run's cgroup went would replace it.
let guestLive = 0;
let endingUntil = 0;
const ENDING_MS = 2_000;
// What of the root's, besides its runs, can write the folder now: while nothing can, a look takes the
// exec steps in paused rebases as the user's (HookGuard's writing).
const writing = () => guestLive > 0 || performance.now() < endingUntil;
// The root's runs in the guest from their refusal to the look after them: while any is in flight,
// the hook guard leaves paused rebases' todos alone, as a run's own rebase may be working through one.
const runs = new Set<string>();

process.on("message", (raw) => {
  const message = raw as ToHost;
  switch (message.type) {
    case "start":
      if (folder) break;
      start(message).then(
        () => send({ type: "ready", processes: saved?.processes ?? [] }),
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
      // Every kind goes to the helper, which does the file kinds and refuses the rest: nothing runs a command here.
      if (!sameFolder()) send({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
      else helper?.stdin?.write(`${JSON.stringify({ id: message.id, kind: message.kind, args: message.args })}\n`);
      break;
    case "cancel":
      // A guest's run cancelled while its refusal was asked: no after comes for it.
      runs.delete(message.id);
      helper?.stdin?.write(`${JSON.stringify({ cancel: message.id })}\n`);
      break;
    case "refusal":
      // A command for a folder replaced since the start would run on the replacement.
      if (!sameFolder()) send({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
      else {
        if (message.run) runs.add(message.id);
        void guard?.refusal().then((refused) => {
          if (refused) runs.delete(message.id);
          if (!failing) send({ type: "result", id: message.id, outcome: refused ?? { ok: null } });
        });
      }
      break;
    case "after":
      runs.delete(message.id);
      if (!("ok" in message.outcome) || (message.outcome.ok as { timed_out?: boolean } | null)?.timed_out) endingUntil = performance.now() + ENDING_MS;
      void guard?.after(message.outcome).then((outcome) => {
        watchHooks();
        if (!failing) send({ type: "result", id: message.id, outcome });
      });
      break;
    case "handles":
      // What the guest's processes can write, they write at any time: the look every WATCH_MS goes on, as after a command.
      guestLive = message.live;
      try {
        save({ processes: message.handles });
      } catch {
        // Kept from the last write.
      }
      watchHooks();
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
  // One host per folder.
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
  const inherited = killed?.hooks ? new Map(Object.entries(killed.hooks)) : null;
  // The root's processes in the guest the last host kept: each still running ended when the app quit,
  // and one that ended by itself keeps how. The guest's registry answers for them until the cloud would forget them.
  const now = Date.now() / 1000;
  const ended = lostWith((last?.processes ?? []).filter((handle) => now - handle.started_at <= FINISHED_TTL_SECONDS), APP_QUIT);
  // On disk before the helper starts, with a killed host's baseline kept: commands
  // wait for the guard, which records its own once it knows it.
  saved = { state: "running", hooks: killed?.hooks ?? null, processes: ended };
  writeRecord(record, saved);
  recordPath = record;
  const running = (hooks: ReadonlyMap<string, string>) => save({ hooks: Object.fromEntries(hooks) });
  // Its first look finds the user's own hooks, while srt starts. After a killed host,
  // that host's are the user's, and the look catches what was left in the folder meanwhile.
  // A command writes the folder alone: a hook linked into it is a command's.
  guard = new HookGuard(path, { inherited, known: running, writable: [path], writing, running: () => runs.size > 0 });
  mkdirSync(tmp, { recursive: true });
  makeCaches(tmp);
  const env = commandEnvironment(message.env, tmp);
  // srt sets the sandbox's TMPDIR from this; its default is shared by every sandbox.
  process.env.CLAUDE_CODE_TMPDIR = tmp;
  // srt and the shell it wraps the helper in run outside the sandbox, and srt itself
  // looks up which, rg and the shell through this process's PATH. Only absolute entries
  // outside the folder and the temp folder are kept, and srt's own tools go by absolute
  // path: no program a command wrote can run out here.
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
    if (isIP(domain.replace(/^\[(.*)\]$/, "$1")) && ((await reach(domain).catch(() => null))?.reach ?? "own") === "own") continue;
    domains.push(domain);
  }
  const policy = sandboxPolicy({ folder: path, tmp, home, appDirs, bwrapPath, socatPath, rgPath, domains });
  // The helper makes no connection: one that came would be refused.
  await SandboxManager.initialize(policy, () => Promise.resolve(false));
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
      // A helper that dies takes its host with it, srt cleaned up and what it was
      // asked unanswered: the app answers that as interrupted and starts a new host
      // for the next operation.
      else if (!stopping) void stop(1);
    });
  });
  // The helper's is the one wrap: srt clears the placeholders it made for it in the temp folder.
  SandboxManager.cleanupAfterCommand();
}

// After a command of the root's in the guest, a look every WATCH_MS until the host stops:
// what it left running there can write a hook at any time.
function watchHooks(): void {
  if (watching || !guard || stopping) return;
  const hooks = guard;
  watching = setTimeout(() => void (async () => {
    // A folder replaced since the start is not this chat's: no look goes over it.
    if (!sameFolder()) return void stop(1);
    await hooks.watch();
    watching = null;
    watchHooks();
  })(), WATCH_MS);
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

async function stop(code = 0): Promise<void> {
  if (stopping) return;
  stopping = true;
  failing = code !== 0;
  if (watching) clearTimeout(watching);
  helper?.kill("SIGKILL");
  // What a command in the guest left, before the record can say the host stopped
  // cleanly. A look that could not see the whole folder, or a host killed during it,
  // leaves "running" and the baseline for the next host. So does a folder moved or
  // replaced: the look would change the hooks of a folder that is not this chat's.
  const clean = sameFolder() && ((await guard?.settle()) ?? true);
  await leave(code, clean);
}

// The way out: srt removes its sockets, then the record says the host stopped
// cleanly, if it did, so the next one has nothing to clear.
async function leave(code: number, clean: boolean): Promise<void> {
  await SandboxManager.reset().catch(() => {});
  try {
    if (recordPath && clean) writeRecord(recordPath, { state: "stopped", hooks: null, processes: saved?.processes ?? [] });
  } finally {
    lock?.close();
    process.exit(code);
  }
}
