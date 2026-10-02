// A tool host: one process per root session, holding that folder's sandbox. srt
// keeps its configuration in module globals, so each folder gets its own process
// (spec, Section 1). It starts the file helper inside the sandbox and relays
// operations to it. A Node child process with an IPC channel.

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import type { Outcome } from "../link/protocol.js";
import { inside } from "../files/paths.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "./messages.js";
import { GLOB, sandboxPolicy } from "./policy.js";

const HELPER = fileURLToPath(new URL("../files/helper.js", import.meta.url));
const READY_TIMEOUT_MS = 15_000;
const SYSTEM_FOLDERS = ["/proc", "/sys", "/dev", "/run"];
const CREDENTIALS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];

const send = (message: FromHost, then?: () => void) => {
  process.send?.(message, undefined, undefined, then);
};
const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

let helper: ChildProcess | null = null;
let folder: { path: string; dev: number; ino: number } | null = null;
let stopping = false;

process.on("message", (raw) => {
  const message = raw as ToHost;
  switch (message.type) {
    case "start":
      if (folder) break;
      start(message).then(
        () => send({ type: "ready" }),
        (error: unknown) => send(
          { type: "failed", message: error instanceof Error ? error.message : String(error) },
          () => process.exit(1),
        ),
      );
      break;
    case "op":
      if (!sameFolder()) send({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
      else helper?.stdin?.write(`${JSON.stringify({ id: message.id, kind: message.kind, args: message.args })}\n`);
      break;
    case "cancel":
      helper?.stdin?.write(`${JSON.stringify({ cancel: message.id })}\n`);
      break;
    case "stop":
      void stop();
      break;
  }
});
process.on("disconnect", () => void stop());
process.on("SIGTERM", () => void stop());

// A path as spelled, and as the file system resolves it when it exists.
function spellings(path: string): string[] {
  const plain = resolve(path);
  try {
    const real = realpathSync(plain);
    return real === plain ? [plain] : [plain, real];
  } catch {
    return [plain];
  }
}

async function start(message: HostStart): Promise<void> {
  const home = message.env.HOME;
  if (!home) throw new Error("the app's environment has no HOME");
  if (!isAbsolute(message.tmp)) throw new Error(`the temp folder must be an absolute path: ${message.tmp}`);
  const tmp = resolve(message.tmp);
  const appDirs = message.appDirs.map((dir) => resolve(dir));
  const path = realpathSync(message.folder);
  const globbed = [path, tmp, ...appDirs].find((entry) => GLOB.test(entry));
  if (globbed) throw new Error(`this computer cannot sandbox a folder whose path holds *, ?, [ or ]: ${globbed}`);
  if (SYSTEM_FOLDERS.some((dir) => inside(path, dir))) {
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
  const { dev, ino } = statSync(path);
  folder = { path, dev, ino };
  mkdirSync(tmp, { recursive: true });
  // srt sets the sandbox's TMPDIR from this; its default is shared by every sandbox.
  process.env.CLAUDE_CODE_TMPDIR = tmp;
  const policy = sandboxPolicy({ folder: path, tmp, home, appDirs, bwrapPath: message.bwrapPath });
  await SandboxManager.initialize(policy);
  // A warning names a protection that is missing, such as seccomp's unix-socket filter: fail closed.
  const { errors, warnings } = SandboxManager.checkDependencies();
  if (errors.length || warnings.length) throw new Error([...errors, ...warnings].join("; "));
  // srt's proxy bridges listen in this process's /tmp, which the policy hides.
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
  const [file, ...args] = argv;
  if (!file) throw new Error("srt returned no command");
  // The app-built environment only: srt's returned env is this process's own.
  const child = spawn(file, args, {
    cwd: path,
    env: { ...message.env, TMPDIR: tmp, SUROGATE_FOLDER: path, ELECTRON_RUN_AS_NODE: "1" },
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
      // A helper that dies takes its host with it: the app answers whatever was
      // running as interrupted and starts a new host for the next operation.
      else if (!stopping) process.exit(1);
    });
  });
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

async function stop(): Promise<void> {
  if (stopping) return;
  stopping = true;
  helper?.kill("SIGKILL");
  await SandboxManager.reset();
  process.exit(0);
}
