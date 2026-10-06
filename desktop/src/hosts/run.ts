// The run kind on the host: one command in the folder's sandbox. Until the root
// has a session runner, the host wraps and spawns each command, so the host is the
// direct parent of srt's bwrap and the command dies with it; killing that bwrap
// ends everything the command started. Once it has one, the command runs in the
// runner (session-runner.ts). Either way it is answered as the guest agent
// answers it (guest/command.ts).

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, rmdirSync } from "node:fs";
import { join } from "node:path";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import {
  answered, CANCELLED, cannotEnter, type CommandChild, type CommandEnd, type Place, ran, runArgs, supervise, unenterable, workdir,
} from "../guest/command.js";
import type { SessionRunner } from "../guest/runner-process.js";
import type { Outcome } from "../link/protocol.js";
import { hideSrtTmp } from "./policy.js";

export interface CommandContext extends Place {
  env: Record<string, string>;
  // The folder had no .claude when the host started (see acquire).
  claudeWasAbsent: boolean;
}

const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));

// Commands from the start of their wrap to srt's cleanup.
let active = 0;

// srt 0.0.77 decides per wrap whether .claude exists, while a concurrent command's
// bwrap may be creating it: the wrap then fails in bwrap (EROFS), or its cleanup
// leaves an empty .claude/ behind. While any command is wrapping or running, a
// folder that had no .claude gets an empty one, so every wrap sees the same; it
// goes when the last command is done (rmdir refuses anything that is not empty).
export function acquire(context: CommandContext): void {
  active += 1;
  if (active > 1 || !context.claudeWasAbsent) return;
  try {
    mkdirSync(join(context.folder, ".claude"));
  } catch {
    // There already.
  }
}

// *wrapped*: srt counted the command (a wrap that rejects has released itself).
export function release(context: CommandContext, wrapped = true): void {
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
export function runCommand(
  args: Record<string, unknown>, context: CommandContext, signal: AbortSignal, id: string, runner: SessionRunner | null = null,
): Promise<Outcome> {
  return answered(() => run(args, context, signal, id, runner));
}

async function run(
  args: Record<string, unknown>, context: CommandContext, signal: AbortSignal, id: string, runner: SessionRunner | null,
): Promise<Outcome> {
  const checked = runArgs(args);
  if (!("command" in checked)) return checked;
  const { command, timeout } = checked;
  const cwd = workdir(context, checked.workdir);
  const code = unenterable(cwd);
  if (code) return ran(cannotEnter(code, cwd), -1);
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
