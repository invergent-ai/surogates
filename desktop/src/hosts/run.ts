// The run kind: one command in the folder's sandbox, answered as the cloud's
// LocalWorkspaceIO.run and the reference laptop answer it. Until the root has a
// session runner, the host wraps and spawns each command, so the host is the
// direct parent of srt's bwrap and the command dies with it; killing that bwrap
// ends everything the command started. Once it has one, the command runs in the
// runner (session-runner.ts), with the same answers.

import { type ChildProcess, spawn } from "node:child_process";
import { accessSync, constants, mkdirSync, rmdirSync, statSync } from "node:fs";
import { constants as osConstants } from "node:os";
import { join } from "node:path";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import { Failure, MAX_MESSAGE_CHARS, osError, sandboxError, valueError } from "../files/answers.js";
import { resolveInFolder } from "../files/paths.js";
import type { Outcome } from "../link/protocol.js";
import { commandOutput, Window } from "./output.js";
import { hideSrtTmp } from "./policy.js";
import type { SessionRunner } from "./session-runner.js";

export interface CommandContext {
  folder: string;
  home: string;
  env: Record<string, string>;
  // The folder had no .claude when the host started (see acquire).
  claudeWasAbsent: boolean;
}

// The workdirs the cloud reads as the folder itself (local.py _HOME_ALIASES).
const HOME_ALIASES = new Set(["$HOME", "~", "$WORKSPACE_DIR", "${HOME}", "${WORKSPACE_DIR}"]);
const MAX_TIMER_MS = 2 ** 31 - 1;
export const CANCELLED: Outcome = { error: { type: "cancelled", message: "The session stopped this command" } };
export const SANDBOX_STOPPED: Outcome = {
  error: { type: "interrupted", message: "interrupted: the computer's sandbox stopped while this ran. Check what it did before repeating it." },
};

// How a command ended: it exited, it never started, or the sandbox it ran in went first.
export type CommandEnd = { code: number | null; signal: NodeJS.Signals | null } | { failed: string } | { lost: true };

// What run needs of a running command, in a sandbox of its own or in the session runner.
export interface CommandChild {
  // Each chunk of its output, as it comes; err: from its stderr.
  onOutput(listener: (chunk: Buffer, err: boolean) => void): void;
  // Once: after all its output, or at once after kill.
  onEnd(listener: (end: CommandEnd) => void): void;
  // Ends it and everything it started.
  kill(): void;
}

const ran = (output: string, returncode: number, timed_out = false): Outcome => ({ ok: { output, returncode, timed_out } });
const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));

// Commands from the start of their wrap to srt's cleanup.
let active = 0;

// srt 0.0.77 decides per wrap whether .claude exists, while a concurrent command's
// bwrap may be creating it: the wrap then fails in bwrap (EROFS), or its cleanup
// leaves an empty .claude/ behind. While any command is wrapping or running, a
// folder that had no .claude gets an empty one, so every wrap sees the same; it
// goes when the last command is done (rmdir refuses anything that is not empty).
function acquire(context: CommandContext): void {
  active += 1;
  if (active > 1 || !context.claudeWasAbsent) return;
  try {
    mkdirSync(join(context.folder, ".claude"));
  } catch {
    // There already.
  }
}

// *wrapped*: srt counted the command (a wrap that rejects has released itself).
function release(context: CommandContext, wrapped = true): void {
  if (wrapped) SandboxManager.cleanupAfterCommand();
  active -= 1;
  if (active > 0 || !context.claudeWasAbsent) return;
  try {
    rmdirSync(join(context.folder, ".claude"));
  } catch {
    // Not there, or not empty.
  }
}

// Never rejects: whatever goes wrong is an outcome.
// *runner*: the root's session runner, once it is up; else the command gets a sandbox of its own.
export async function runCommand(
  args: Record<string, unknown>, context: CommandContext, signal: AbortSignal, id: string, runner: SessionRunner | null = null,
): Promise<Outcome> {
  try {
    const outcome = await run(args, context, signal, id, runner);
    if (JSON.stringify(outcome).length > MAX_MESSAGE_CHARS) {
      return { error: { type: "too_large", message: "The result of run is too large" } };
    }
    return outcome;
  } catch (error) {
    return {
      error: error instanceof Failure
        ? error.refusal
        : { type: "other", message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) },
    };
  }
}

async function run(
  args: Record<string, unknown>, context: CommandContext, signal: AbortSignal, id: string, runner: SessionRunner | null,
): Promise<Outcome> {
  const { command, workdir: requested, timeout } = args;
  if (typeof command !== "string") throw valueError("'command' must be a string");
  if (requested !== null && typeof requested !== "string") throw valueError("'workdir' must be a string or null");
  if (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout <= 0) {
    throw valueError("'timeout' must be a positive number");
  }
  if (command.includes("\0")) return ran("embedded null byte", -1);
  const cwd = workdir(context, requested);
  const unusable = cannotEnter(cwd);
  if (unusable) return ran(unusable, -1);
  if (runner) {
    if (signal.aborted) return CANCELLED;
    return supervise(runner.spawn({ id, command, cwd, env: {}, pty: false, stdin: false }), timeout, signal);
  }
  let argv: string[];
  acquire(context);
  try {
    ({ argv } = await SandboxManager.wrapWithSandboxArgv(command, undefined, undefined, signal, undefined, { commandId: id }));
  } catch (error) {
    // A wrap that rejects has released srt's count itself.
    release(context, false);
    return ran(describe(error), -1);
  }
  // From here on srt counts this command until release, which runs exactly once,
  // after its bwrap has gone (or never started).
  const [shell, flag, line] = argv;
  if (signal.aborted || !shell || flag === undefined || line === undefined) {
    release(context);
    return signal.aborted ? CANCELLED : ran("srt returned no command", -1);
  }
  let hidden: string;
  try {
    hidden = hideSrtTmp(line);
  } catch (error) {
    release(context);
    return ran(describe(error), -1);
  }
  let child: ChildProcess;
  try {
    // --norc --noprofile: never the user's startup files out here, whatever stdin is.
    child = spawn(shell, ["--norc", "--noprofile", flag, hidden], { cwd, env: context.env, stdio: ["ignore", "pipe", "pipe"] });
  } catch (error) {
    // A spawn that throws (an argument it refuses) never started bwrap: release it all the same.
    release(context);
    return ran(describe(error), -1);
  }
  return supervise(own(child), timeout, signal, () => release(context));
}

// A command in a sandbox of its own: bwrap is this host's child, and killing it
// ends everything in the sandbox, which then has nothing more to say.
function own(child: ChildProcess): CommandChild {
  let killed = false;
  let ended: CommandEnd | null = null;
  let listener: ((end: CommandEnd) => void) | null = null;
  const end = (value: CommandEnd) => {
    if (ended) return;
    ended = value;
    listener?.(value);
  };
  child.on("error", (error) => end({ failed: describe(error) }));
  child.on("exit", (code, signal) => {
    if (killed) end({ code, signal });
  });
  child.on("close", (code, signal) => end({ code, signal }));
  return {
    onOutput: (output) => {
      child.stdout?.on("data", (chunk: Buffer) => output(chunk, false));
      child.stderr?.on("data", (chunk: Buffer) => output(chunk, true));
    },
    onEnd: (next) => {
      listener = next;
      if (ended) next(ended);
    },
    kill: () => {
      killed = true;
      try {
        child.kill("SIGKILL");
      } catch {
        // Already gone.
      }
    },
  };
}

// One command's answer, wherever it runs: its output and exit code, or its
// timeout or cancel, once. *done* runs as it ends.
function supervise(child: CommandChild, timeout: number, signal: AbortSignal, done: () => void = () => {}): Promise<Outcome> {
  return new Promise<Outcome>((resolve) => {
    const out = new Window();
    const err = new Window();
    let timedOut = false;
    const onAbort = () => child.kill();
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill();
    }, Math.min(timeout * 1000, MAX_TIMER_MS));
    signal.addEventListener("abort", onAbort, { once: true });
    child.onOutput((chunk, isErr) => (isErr ? err : out).push(chunk));
    child.onEnd((end) => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      done();
      if ("failed" in end) resolve(ran(end.failed, -1));
      else if ("lost" in end) resolve(SANDBOX_STOPPED);
      else if (timedOut) resolve(ran(`Command timed out after ${timeout} seconds`, 124, true));
      else if (signal.aborted) resolve(CANCELLED);
      else {
        out.end();
        err.end();
        resolve(ran(commandOutput(out, err), end.code ?? 128 + (end.signal ? osConstants.signals[end.signal] : 0)));
      }
    });
  });
}

// _workdir: an alias is the folder, anything else must resolve inside it.
export function workdir({ folder, home }: CommandContext, requested: string | null): string {
  const path = requested && HOME_ALIASES.has(requested) ? folder : requested;
  try {
    return resolveInFolder(folder, home, path ?? "");
  } catch (error) {
    if (error instanceof Failure && error.refusal.type === "sandbox") {
      throw sandboxError(`Blocked: ${error.refusal.message} All commands must run within the workspace directory.`);
    }
    throw error;
  }
}

// Why a command cannot run in *cwd*: an errno name, or null.
export function unenterable(cwd: string): string | null {
  try {
    if (!statSync(cwd).isDirectory()) return "ENOTDIR";
    accessSync(cwd, constants.X_OK);
    return null;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code ?? "EIO";
  }
}

// What the cloud's subprocess call says when the folder cannot be entered: str(OSError).
function cannotEnter(cwd: string): string | null {
  const code = unenterable(cwd);
  if (!code) return null;
  const errno = osConstants.errno[code as keyof typeof osConstants.errno];
  return `${errno === undefined ? "" : `[Errno ${errno}] `}${osError(code, cwd).refusal.message}`;
}
