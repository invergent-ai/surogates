import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { SpawnRequest } from "../src/hosts/messages.js";
import type { CommandEnd } from "../src/hosts/run.js";
import { type RunnerChild, SessionRunner } from "../src/hosts/session-runner.js";

const RUNNER = fileURLToPath(new URL("../dist/hosts/runner.js", import.meta.url));

let base: string;
let runners: SessionRunner[];
let raw: ChildProcess[];
let next = 0;

// The runner as the host starts it, without srt: the protocol is the same.
function bare(): ChildProcess {
  const child = spawn(process.execPath, [RUNNER], {
    cwd: base,
    env: { PATH: "/usr/bin:/bin", HOME: base, LANG: "C.UTF-8", ELECTRON_RUN_AS_NODE: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  raw.push(child);
  return child;
}

async function runner(onLost?: () => void): Promise<SessionRunner> {
  const started = new SessionRunner(bare(), onLost);
  runners.push(started);
  await started.ready;
  return started;
}

// Ids of their own: a bare runner's sweep reads every process's marker, other test files' too.
const request = (command: string, background = false, extra: Partial<SpawnRequest> = {}): SpawnRequest => ({
  id: `sr-${next++}`, command, cwd: base, env: {}, pty: false, stdin: background, ...extra,
});

// Everything a command says, and how it ended.
function collect(child: RunnerChild): Promise<{ out: string; err: string; end: CommandEnd }> {
  let out = "";
  let err = "";
  child.onOutput((chunk, isErr) => {
    if (isErr) err += chunk.toString();
    else out += chunk.toString();
  });
  return new Promise((resolve) => child.onEnd((end) => resolve({ out, err, end })));
}

const running = (pattern: string) => Number(spawnSync("pgrep", ["-fc", pattern], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "session-runner-")));
  runners = [];
  raw = [];
});

afterEach(async () => {
  for (const started of runners) await started.stop();
  for (const child of raw) child.kill("SIGKILL");
  // A bare runner has no pid namespace: what it ran outlives one that is killed.
  spawnSync("pkill", ["-KILL", "-f", "^sleep 6(48|57)$"]);
  rmSync(base, { recursive: true, force: true });
});

describe("the session runner", { timeout: 20_000 }, () => {
  it("runs a command and answers its output, each stream apart, and how it ended", async () => {
    const started = await runner();
    expect(await collect(started.spawn(request("echo out; echo err >&2; exit 3")))).toEqual({
      out: "out\n", err: "err\n", end: { code: 3, signal: null },
    });
    expect((await collect(started.spawn(request("kill -9 $$")))).end).toEqual({ code: null, signal: "SIGKILL" });
  });

  it("merges a background process's stderr into its stdout, in the order it was written", async () => {
    const started = await runner();
    expect(await collect(started.spawn(request("echo a; echo b >&2; echo c; (exit 4)", true)))).toEqual({
      out: "a\nb\nc\n", err: "", end: { code: 4, signal: null },
    });
  });

  it("gives a command the runner's environment, the host's names and its marker, without Electron's flag", async () => {
    const started = await runner();
    const child = started.spawn(request("env", false, { env: { EXTRA: "1" } }));
    const lines = (await collect(child)).out.split("\n");
    expect(lines).toContain(`SUROGATE_PROCESS=${child.id}`);
    expect(lines).toContain("EXTRA=1");
    expect(lines).toContain(`HOME=${base}`);
    expect(lines.filter((line) => line.startsWith("ELECTRON_RUN_AS_NODE="))).toEqual([]);
  });

  it("ends what a run leaves behind when its shell exits, setsid or not", async () => {
    const started = await runner();
    const begun = Date.now();
    expect(await collect(started.spawn(request("sleep 641 & setsid sleep 642 & echo started")))).toEqual({
      out: "started\n", err: "", end: { code: 0, signal: null },
    });
    expect(Date.now() - begun).toBeLessThan(5_000);
    await until(() => running("^sleep 64[12]$") === 0);
  });

  it("keeps what a background process starts, and kills all of it, setsid and double forks too", async () => {
    const started = await runner();
    const child = started.spawn(request("sleep 643 & setsid sleep 644 & (sleep 645 &); sleep 646", true));
    const ended = collect(child);
    expect(await child.started).toEqual(expect.any(Number));
    await until(() => running("^sleep 64[3-6]$") === 4);
    child.kill();
    expect((await ended).end).toEqual({ code: null, signal: "SIGKILL" });
    await until(() => running("^sleep 64[3-6]$") === 0);
  });

  it("sends a signal to a background process's group", async () => {
    const started = await runner();
    const child = started.spawn(request("sleep 647", true));
    const ended = collect(child);
    await child.started;
    child.signal("SIGTERM");
    expect((await ended).end).toEqual({ code: null, signal: "SIGTERM" });
  });

  it("writes to a background process's stdin", async () => {
    const started = await runner();
    const child = started.spawn(request("head -n 1", true));
    const ended = collect(child);
    await child.started;
    child.write(Buffer.from("hello\nnot this\n"));
    expect(await ended).toEqual({ out: "hello\n", err: "", end: { code: 0, signal: null } });
  });

  it("answers a command it cannot start", async () => {
    const started = await runner();
    const child = started.spawn(request("true", false, { cwd: join(base, "gone") }));
    expect(await child.started).toBeNull();
    expect((await collect(child)).end).toEqual({ failed: expect.stringContaining("ENOENT") });
  });

  it("ends what ran in it as lost when it dies, and says so once", async () => {
    let lost = 0;
    const started = await runner(() => {
      lost += 1;
    });
    const child = started.spawn(request("sleep 648", true));
    const ended = collect(child);
    await child.started;
    raw[0]?.kill("SIGKILL");
    expect((await ended).end).toEqual({ lost: true });
    await started.gone;
    expect(lost).toBe(1);
    expect((await collect(started.spawn(request("true")))).end).toEqual({ lost: true });
  });

  it("stops when the host ends its stdin, and its commands with it", async () => {
    let lost = 0;
    const started = await runner(() => {
      lost += 1;
    });
    const child = started.spawn(request("sleep 649", true));
    await child.started;
    await started.stop();
    expect(lost).toBe(0);
    await until(() => running("^sleep 649$") === 0);
  });

  it("leaves a flood in the command's pipe while the host does not read", async () => {
    // Read nothing: the runner's stdout pipe fills, and the runner must stop reading the command.
    const child = bare();
    child.stdout?.pause();
    const flood: SpawnRequest = request("head -c 300000000 /dev/zero", true);
    child.stdin?.write(`${JSON.stringify({ type: "spawn", ...flood })}\n`);
    await until(() => running("^head -c 300000000 /dev/zero$") === 1);
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    const rss = Number(/VmRSS:\s+(\d+) kB/.exec(readFileSync(`/proc/${child.pid}/status`, "utf8"))?.[1]) * 1024;
    expect(rss).toBeLessThan(200 * 1024 * 1024);
    // Still there, blocked on its pipe.
    expect(running("^head -c 300000000 /dev/zero$")).toBe(1);
    child.stdin?.end();
    await until(() => running("^head -c 300000000 /dev/zero$") === 0);
  });
});
