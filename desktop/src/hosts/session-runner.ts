// The host's side of a root's session runner (guest/runner.ts): the runner is
// wrapped once with srt, from the folder, so srt's denies hold inside it for its
// whole life, and every command in it shares one network namespace.

import { type ChildProcess, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

import { SessionRunner } from "../guest/runner-process.js";
import { hideSrtTmp, quote } from "./policy.js";
import { acquire, type CommandContext, release } from "./run.js";

export const RUNNER = fileURLToPath(new URL("../guest/runner.js", import.meta.url));

// How the host ends its runner before its last look, which must find the runner
// gone: one that is up is stopped, and its SIGKILL bounds that. One still starting
// gets *ms*: ToolHosts kills a host that takes too long, and the next clears up after it.
export async function stopRunner(
  starting: Promise<Pick<SessionRunner, "stop">> | null, up: Pick<SessionRunner, "stop"> | null, ms: number,
): Promise<void> {
  if (up) return up.stop();
  await Promise.race([
    starting?.then((started) => started.stop(), () => {}),
    new Promise((resolve) => setTimeout(resolve, ms)),
  ]);
}

// Write rules for one runner's wrap, on top of the host's own.
export interface RunnerPaths {
  denyWrite: readonly string[];
  allowWrite: readonly string[];
}

// A root's runner, wrapped with srt from the host's working folder, which is the
// session folder, and spawned as the host spawns a command: the app-built
// environment, srt's outer bash without startup files, /tmp/claude hidden.
// srt counts it as one command for its whole life, and its placeholders stay in
// the folder while it lives; it is released once, after its bwrap has gone.
export async function startRunner(
  context: CommandContext, onLost: () => void, paths: RunnerPaths = { denyWrite: [], allowWrite: [] },
): Promise<SessionRunner> {
  const filesystem = SandboxManager.getConfig()?.filesystem;
  if (!filesystem) throw new Error("the sandbox is not set up");
  // A wrap's own filesystem rules replace the host's whole, so they start from them: /tmp/claude's deny among them.
  const custom = {
    filesystem: {
      ...filesystem,
      allowWrite: [...filesystem.allowWrite, ...paths.allowWrite],
      denyWrite: [...filesystem.denyWrite, ...paths.denyWrite],
    },
  };
  acquire(context);
  let argv: string[];
  try {
    ({ argv } = await SandboxManager.wrapWithSandboxArgv(`${quote(process.execPath)} ${quote(RUNNER)}`, undefined, custom));
  } catch (error) {
    // A wrap that rejects has released srt's count itself.
    release(context, false);
    throw error;
  }
  let child: ChildProcess;
  try {
    const [file, flag, line] = argv;
    if (!file || flag === undefined || line === undefined) throw new Error("srt returned no command");
    child = spawn(file, ["--norc", "--noprofile", flag, hideSrtTmp(line)], {
      cwd: context.folder,
      env: { ...context.env, ELECTRON_RUN_AS_NODE: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (error) {
    release(context);
    throw error;
  }
  const runner = new SessionRunner(child, onLost);
  void runner.gone.then(() => release(context));
  try {
    await runner.ready;
  } catch (error) {
    await runner.stop();
    throw error;
  }
  return runner;
}
