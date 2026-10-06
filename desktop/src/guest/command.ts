// The run kind's answers, wherever the command runs: its arguments checked as
// the cloud checks them, its folder, and its output, timeout and cancel, as the
// cloud's LocalWorkspaceIO.run and the reference laptop answer them. The guest
// agent runs every command in a root runner; until commands move into the VM, a
// tool host runs them too (hosts/run.ts).

import { accessSync, constants, statSync } from "node:fs";
import { constants as osConstants } from "node:os";

import { Failure, MAX_MESSAGE_CHARS, osError, sandboxError, valueError } from "../files/answers.js";
import { resolveInFolder } from "../files/paths.js";
import type { Outcome } from "../link/protocol.js";
import { commandOutput, Window } from "./output.js";

// Where a root's commands run: its folder, and the home ~ expands to.
export interface Place {
  folder: string;
  home: string;
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

// What run needs of a running command, in a sandbox of its own or in a runner.
export interface CommandChild {
  // Each chunk of its output, as it comes; err: from its stderr.
  onOutput(listener: (chunk: Buffer, err: boolean) => void): void;
  // Once: after all its output, or at once after kill.
  onEnd(listener: (end: CommandEnd) => void): void;
  // Ends it and everything it started.
  kill(): void;
}

export const ran = (output: string, returncode: number, timed_out = false): Outcome => ({ ok: { output, returncode, timed_out } });
export const timedOut = (timeout: number): Outcome => ran(`Command timed out after ${timeout} seconds`, 124, true);

// Never rejects: whatever goes wrong is an outcome, and one too large for a message is refused.
export async function answered(run: () => Promise<Outcome>): Promise<Outcome> {
  try {
    const outcome = await run();
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

// run's arguments, checked in the cloud's order, or the answer when the command cannot run.
export function runArgs(args: Record<string, unknown>): { command: string; workdir: string | null; timeout: number } | Outcome {
  const { command, workdir, timeout } = args;
  if (typeof command !== "string") throw valueError("'command' must be a string");
  if (workdir !== null && typeof workdir !== "string") throw valueError("'workdir' must be a string or null");
  if (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout <= 0) {
    throw valueError("'timeout' must be a positive number");
  }
  if (command.includes("\0")) return ran("embedded null byte", -1);
  return { command, workdir, timeout };
}

// One command's answer, wherever it runs: its output and exit code, or its
// timeout or cancel, once. *done* runs as it ends.
export function supervise(child: CommandChild, timeout: number, signal: AbortSignal, done: () => void = () => {}): Promise<Outcome> {
  return new Promise<Outcome>((resolve) => {
    const out = new Window();
    const err = new Window();
    let expired = false;
    const onAbort = () => child.kill();
    const timer = setTimeout(() => {
      expired = true;
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
      else if (expired) resolve(timedOut(timeout));
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
export function workdir({ folder, home }: Place, requested: string | null): string {
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
export function cannotEnter(code: string, cwd: string): string {
  const errno = osConstants.errno[code as keyof typeof osConstants.errno];
  return `${errno === undefined ? "" : `[Errno ${errno}] `}${osError(code, cwd).refusal.message}`;
}
