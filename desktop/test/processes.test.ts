import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setFlagsFromString } from "node:v8";
import { runInNewContext } from "node:vm";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Failure, NUL_REFUSED, OUTPUT_CAP_CHARS, pyJsonLength, sandboxError } from "../src/files/answers.js";
import type { Outcome } from "../src/link/protocol.js";
import {
  APP_QUIT, MAX_PROCESSES, OUT_OF_MEMORY, type ProcessHandle, Processes, type ProcessesOptions, RUNNER_GONE, type Spawner, TOO_MANY,
} from "../src/guest/processes.js";
import { CANCELLED, type CommandEnd, unenterable, workdir } from "../src/guest/command.js";
import { SessionRunner } from "../src/guest/runner-process.js";

const RUNNER = fileURLToPath(new URL("../dist/guest/runner.js", import.meta.url));

let base: string;
let runners: SessionRunner[];
let children: ChildProcess[];
let registry: Processes;

// The root runner without its namespaces, as the tests start it; PATH is what the commands find.
async function runner(PATH = "/usr/bin:/bin"): Promise<SessionRunner> {
  const child = spawn(process.execPath, [RUNNER], {
    cwd: base, env: { PATH, HOME: base, LANG: "C.UTF-8" }, stdio: ["pipe", "pipe", "pipe"],
  });
  const started = new SessionRunner(child);
  children.push(child);
  runners.push(started);
  await started.ready;
  return started;
}

function processes(options: Partial<ProcessesOptions> = {}): Processes {
  let up: Promise<SessionRunner> | null = null;
  registry = new Processes({
    // As the runner answers place, in the test's own view.
    place: async (requested) => {
      const cwd = workdir({ folder: base, home: base }, requested);
      return { cwd, unenterable: unenterable(cwd) };
    },
    runner: () => (up ??= runner()),
    ...options,
  });
  return registry;
}

// A runner whose children the test drives: each starts with *pid*, or with none
// after ending {failed} when *fails* is given.
function fake({ pid = 7, fails }: { pid?: number; fails?: string } = {}) {
  const spawned: { output(text: string): void; end(end: CommandEnd): void }[] = [];
  const spawner: Spawner = {
    spawn() {
      const outputs: ((chunk: Buffer, err: boolean) => void)[] = [];
      const ends: ((end: CommandEnd) => void)[] = [];
      const end = (value: CommandEnd) => { for (const listener of ends) listener(value); };
      spawned.push({ output: (text) => { for (const listener of outputs) listener(Buffer.from(text), false); }, end });
      return {
        started: Promise.resolve().then(() => (fails === undefined
          ? pid
          : new Promise((resolve) => setTimeout(() => { end({ failed: fails }); resolve(null); }, 0)))),
        onOutput: (listener) => { outputs.push(listener); },
        onEnd: (listener) => { ends.push(listener); },
        kill() {}, signal() {}, write: async () => null,
      };
    },
  };
  return { runner: async () => spawner, spawned };
}

const ask = (kind: string, args: Record<string, unknown>, signal = new AbortController().signal) =>
  registry.answer(kind, args, signal) as Promise<{ ok?: any; error?: { type: string; message: string } }>;
const start = async (command: string, extra: Record<string, unknown> = {}) => {
  const answer = await ask("start", {
    command, workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null, ...extra,
  });
  return answer.ok?.session_id as string;
};
const running = (pattern: string) => Number(spawnSync("pgrep", ["-fc", pattern], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean | Promise<boolean>, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "processes-")));
  runners = [];
  children = [];
});

afterEach(async () => {
  for (const started of runners) await started.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("background processes", { timeout: 20_000 }, () => {
  it("starts a process and answers for it in the cloud's shapes", async () => {
    processes();
    const started = await ask("start", {
      command: "printf 'one\\ntwo\\nthree\\n'; echo err >&2; (exit 3)", workdir: null, task_id: "t", pty: false,
      notify_on_complete: true, watcher_interval: 30,
    });
    expect(started).toEqual({ ok: { session_id: expect.stringMatching(/^proc_[0-9a-f]{12}$/), pid: expect.any(Number) } });
    const id = started.ok.session_id as string;
    const pid = started.ok.pid as number;
    expect(await ask("wait", { session_id: id, timeout: 10 })).toEqual({
      ok: { status: "exited", exit_code: 3, output: "one\ntwo\nthree\nerr\n" },
    });
    expect(await ask("poll", { session_id: id })).toEqual({
      ok: {
        session_id: id, command: "printf 'one\\ntwo\\nthree\\n'; echo err >&2; (exit 3)", status: "exited", pid,
        uptime_seconds: expect.any(Number), output_preview: "one\ntwo\nthree\nerr\n", exit_code: 3,
      },
    });
    expect(await ask("read_output", { session_id: id, offset: 0, limit: 2 })).toEqual({
      ok: { session_id: id, status: "exited", output: "three\nerr", total_lines: 4, showing: "2 lines" },
    });
    expect((await ask("read_output", { session_id: id, offset: 1, limit: 1 })).ok.output).toBe("two");
    expect((await ask("read_output", { session_id: id, offset: null, limit: null })).ok.showing).toBe("4 lines");
    expect(await ask("list_processes", { task_id: "t" })).toEqual({
      ok: [{
        session_id: id, command: "printf 'one\\ntwo\\nthree\\n'; echo err >&2; (exit 3)", cwd: base, pid,
        started_at: expect.stringMatching(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/), uptime_seconds: expect.any(Number),
        status: "exited", output_preview: "one\ntwo\nthree\nerr\n", exit_code: 3,
      }],
    });
    expect(await ask("list_processes", { task_id: "other" })).toEqual({ ok: [] });
    expect(await ask("kill", { session_id: id })).toEqual({ ok: { status: "already_exited", exit_code: 3 } });
    expect(await ask("write_stdin", { session_id: id, data: "x" })).toEqual({
      ok: { status: "already_exited", error: "Process has already finished" },
    });
  });

  it("answers an id it does not know", async () => {
    processes();
    const missing = { ok: { status: "not_found", error: "No process with ID proc_000000000000" } };
    for (const kind of ["poll", "read_output", "wait", "kill", "write_stdin"]) {
      expect(await ask(kind, { session_id: "proc_000000000000", offset: 0, limit: 200, timeout: 1, data: "" })).toEqual(missing);
    }
  });

  it("shows a process's output as it comes", async () => {
    processes();
    const id = await start("echo ready; sleep 30");
    await until(async () => (await ask("poll", { session_id: id })).ok.output_preview === "ready\n");
    expect((await ask("poll", { session_id: id })).ok.status).toBe("running");
  });

  it("kills a process and everything it started, and answers -15 as the cloud does", async () => {
    processes();
    const id = await start("sleep 661 & sleep 662");
    await until(() => running("^sleep 66[12]$") === 2);
    expect(await ask("kill", { session_id: id })).toEqual({ ok: { status: "killed", session_id: id } });
    expect((await ask("poll", { session_id: id })).ok).toMatchObject({ status: "exited", exit_code: -15 });
    await until(() => running("^sleep 66[12]$") === 0);
  });

  it("kills a process that ignores SIGTERM after two seconds", async () => {
    processes();
    const id = await start("trap '' TERM; sleep 663");
    await until(() => running("^sleep 663$") === 1);
    const begun = Date.now();
    expect(await ask("kill", { session_id: id })).toEqual({ ok: { status: "killed", session_id: id } });
    expect(Date.now() - begun).toBeGreaterThanOrEqual(2_000);
    await until(() => running("^sleep 663$") === 0);
  });

  it("writes to a process's stdin, counting characters as Python does", async () => {
    processes();
    const id = await start("head -n 1");
    expect(await ask("write_stdin", { session_id: id, data: "héllo\n" })).toEqual({ ok: { status: "ok", bytes_written: 6 } });
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok).toEqual({ status: "exited", exit_code: 0, output: "héllo\n" });
  });

  it("waits until the timeout, says when it clamped one, and stops when cancelled", async () => {
    processes();
    const id = await start("sleep 30");
    expect(await ask("wait", { session_id: id, timeout: 1 })).toEqual({
      ok: { status: "timeout", output: "", timeout_note: "Waited 1s, process still running" },
    });
    expect(await ask("wait", { session_id: id, timeout: -1 })).toEqual({
      ok: { status: "timeout", output: "", timeout_note: "Waited -1s, process still running" },
    });
    const controller = new AbortController();
    const waiting = ask("wait", { session_id: id, timeout: 500 }, controller.signal);
    setTimeout(() => controller.abort(), 200);
    expect(await waiting).toEqual(CANCELLED);
    const done = await start("true");
    expect(await ask("wait", { session_id: done, timeout: 500 })).toEqual({
      ok: { status: "exited", exit_code: 0, output: "", timeout_note: "Requested wait of 500s was clamped to configured limit of 180s" },
    });
  });

  it("strips terminal escapes where the cloud does, and leaves them in list's preview", async () => {
    processes();
    const id = await start("printf '\\033[31mred\\033[0m\\n'");
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok.output).toBe("red\n");
    expect((await ask("list_processes", { task_id: null })).ok[0].output_preview).toBe("\u001b[31mred\u001b[0m\n");
  });

  it("keeps the last 200 000 characters of a process's output", async () => {
    processes();
    const id = await start("head -c 300000 /dev/zero | tr '\\000' x; echo; echo END");
    await ask("wait", { session_id: id, timeout: 10 });
    expect(await ask("read_output", { session_id: id, offset: 0, limit: 10 })).toEqual({
      ok: { session_id: id, status: "exited", output: `${"x".repeat(199_995)}\nEND`, total_lines: 2, showing: "2 lines" },
    });
  });

  it("caps every string in an outcome, and makes it well-formed", async () => {
    processes();
    // Control characters cost six each, JSON-encoded: 100 000 of them are over the cap.
    const wide = await start("head -c 100000 /dev/zero | tr '\\000' '\\001'");
    await ask("wait", { session_id: wide, timeout: 10 });
    const output = (await ask("read_output", { session_id: wide, offset: 0, limit: 200 })).ok.output as string;
    expect(output).toContain("chars omitted by the computer");
    expect(pyJsonLength(output)).toBeLessThan(OUTPUT_CAP_CHARS + 200);
    const long = await start(`: ${"x".repeat(1000)}`);
    expect((await ask("list_processes", { task_id: "t" })).ok.find((entry: { session_id: string }) => entry.session_id === long).command)
      .toBe(`: ${"x".repeat(198)}`);
    const lone = await start("echo \ud800");
    expect((await ask("poll", { session_id: lone })).ok.command).toBe("echo \ufffd");
  });

  it("keeps a flood's output bounded and answers while it goes", async () => {
    processes();
    const id = await start("yes");
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const begun = Date.now();
    const page = (await ask("read_output", { session_id: id, offset: 0, limit: 1_000_000 })).ok;
    expect(Date.now() - begun).toBeLessThan(1_000);
    expect(page).toMatchObject({ status: "running", total_lines: expect.any(Number) });
    expect(page.total_lines).toBeLessThanOrEqual(100_000);
    expect(await ask("kill", { session_id: id })).toEqual({ ok: { status: "killed", session_id: id } });
  });

  it("refuses a write to a process that is not reading its input", async () => {
    processes();
    const id = await start("sleep 690");
    const chunk = "x".repeat(512 * 1024);
    let answer: Awaited<ReturnType<typeof ask>> = {};
    // Each write that is taken waits in the runner, up to 1 MiB.
    for (let write = 0; write < 8 && answer.ok?.status !== "error"; write += 1) {
      answer = await ask("write_stdin", { session_id: id, data: chunk });
    }
    expect(answer).toEqual({ ok: { status: "error", error: "The process is not reading its input" } });
    expect((await ask("poll", { session_id: id })).ok.status).toBe("running");
  });

  it("decodes a character split across two writes", async () => {
    processes();
    const id = await start("printf '\\344\\270'; sleep 0.2; printf '\\255\\n'");
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok.output).toBe("中\n");
  });

  it("is running while what it left holds its output, as in the cloud, and kill ends both", async () => {
    processes();
    const id = await start("sleep 667 & echo left");
    await until(async () => (await ask("poll", { session_id: id })).ok.output_preview === "left\n");
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect((await ask("poll", { session_id: id })).ok.status).toBe("running");
    expect(await ask("kill", { session_id: id })).toEqual({ ok: { status: "killed", session_id: id } });
    await until(() => running("^sleep 667$") === 0);
  });

  it(`refuses a process past ${MAX_PROCESSES} running ones`, async () => {
    processes();
    for (let i = 0; i < MAX_PROCESSES; i += 1) expect(await start("sleep 664")).toMatch(/^proc_/);
    expect(await ask("start", { command: "true", workdir: null, task_id: "t", pty: false })).toEqual({
      error: { type: "sandbox", message: TOO_MANY },
    });
  });

  it("drops finished processes 30 minutes after they started, and the oldest finished one at the limit", async () => {
    let now = 1_000_000;
    processes({ now: () => now });
    const first = await start("true");
    await ask("wait", { session_id: first, timeout: 10 });
    now += 1801;
    const second = await start("true");
    expect((await ask("list_processes", { task_id: "t" })).ok.map((entry: { session_id: string }) => entry.session_id)).toEqual([second]);
    await ask("wait", { session_id: second, timeout: 10 });
    for (let i = 0; i < MAX_PROCESSES - 1; i += 1) await start("sleep 665");
    // 63 running and one finished make 64: the next start drops the finished one.
    await start("sleep 665");
    expect(await ask("poll", { session_id: second })).toEqual({ ok: { status: "not_found", error: `No process with ID ${second}` } });
  });

  it("answers a workdir it refuses, one it cannot enter and a NUL as the cloud does", async () => {
    processes();
    const answer = (workdir: string | null, command = "true") => ask("start", { command, workdir, task_id: "t", pty: false });
    expect(await answer("/etc")).toEqual({
      error: {
        type: "sandbox",
        message: `Blocked: Path traversal blocked: '/etc' resolves to '/etc' which is outside the workspace '${base}'. All commands must run within the workspace directory.`,
      },
    });
    expect(await answer("nope")).toEqual({ error: { type: "os", code: "ENOENT", message: `No such file or directory: '${base}/nope'` } });
    expect(await answer(null, "a\0b")).toEqual({ error: { type: "value", message: NUL_REFUSED } });
    expect(await answer("a\0b")).toEqual({ error: { type: "value", message: NUL_REFUSED } });
    // A NUL is refused before the workdir is looked at, in the command or in the workdir itself.
    expect(await answer("nope", "a\0b")).toEqual({ error: { type: "value", message: NUL_REFUSED } });
    expect(await answer("/etc", "a\0b")).toEqual({ error: { type: "value", message: NUL_REFUSED } });
    expect(await answer("/etc/a\0b")).toEqual({ error: { type: "value", message: NUL_REFUSED } });
    // A refusal quotes the workdir as it came, lone surrogate and all.
    expect((await answer("/\ud800")).error?.message).toBe(
      `Blocked: Path traversal blocked: '/\ufffd' resolves to '/\ufffd' which is outside the workspace '${base}'. All commands must run within the workspace directory.`,
    );
  });

  it("asks where its command runs before it starts, and answers that answer's refusal, or a cancel while it waits", async () => {
    let asked = 0;
    const refusal = { type: "interrupted", message: "interrupted: the runner went" };
    processes({ place: async () => { throw new Failure(refusal); }, runner: async () => { asked += 1; throw new Error("not wanted"); } });
    expect(await ask("start", { command: "true", workdir: null, task_id: "t", pty: false })).toEqual({ error: refusal });
    processes({ place: () => new Promise(() => {}), runner: async () => { asked += 1; throw new Error("not wanted"); } });
    const controller = new AbortController();
    const starting = ask("start", { command: "true", workdir: null, task_id: "t", pty: false }, controller.signal);
    setTimeout(() => controller.abort(), 10);
    expect(await starting).toEqual(CANCELLED);
    expect(asked).toBe(0);
  });

  it("refuses what the hook guard refuses, before any runner starts", async () => {
    let asked = 0;
    const refused: Outcome = { error: { type: "sandbox", message: "Blocked: no" } };
    processes({ refusal: async () => refused, runner: async () => { asked += 1; throw new Error("not wanted"); } });
    expect(await ask("start", { command: "true", workdir: null, task_id: "t", pty: false })).toEqual(refused);
    expect(asked).toBe(0);
  });

  it("answers unavailable when the runner cannot start", async () => {
    processes({ runner: async () => { throw new Error("no sandbox"); } });
    expect(await ask("start", { command: "true", workdir: null, task_id: "t", pty: false })).toEqual({
      error: { type: "unavailable", message: "This computer could not start the sandbox for background processes: no sandbox" },
    });
  });

  it("answers the sandbox's own refusal to start a runner as it is", async () => {
    processes({ runner: async () => { throw sandboxError("Blocked: too many protected paths"); } });
    expect(await ask("start", { command: "true", workdir: null, task_id: "t", pty: false })).toEqual({
      error: { type: "sandbox", message: "Blocked: too many protected paths" },
    });
  });

  it("marks what ran in a runner that died as ended, with a note", async () => {
    processes();
    try {
      const id = await start("sleep 666");
      await until(() => running("^sleep 666$") === 1);
      // Bare, the runner leaves its commands behind when it dies; a sandbox's would go with it.
      children[0]?.kill("SIGKILL");
      await until(async () => (await ask("poll", { session_id: id })).ok.status === "exited");
      expect((await ask("poll", { session_id: id })).ok).toMatchObject({ exit_code: null, note: RUNNER_GONE });
      expect((await ask("list_processes", { task_id: "t" })).ok[0]).toMatchObject({ status: "exited", note: RUNNER_GONE });
    } finally {
      spawnSync("pkill", ["-KILL", "-f", "^sleep 666$"]);
    }
  });

  it("notes a process the kernel ended for memory, with the exit code a shell gives its signal", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const id = await start("x");
    spawned[0]?.end({ code: null, signal: "SIGKILL", oom: true });
    expect((await ask("poll", { session_id: id })).ok).toMatchObject({ status: "exited", exit_code: 137, note: OUT_OF_MEMORY });
  });

  it("reads carriage returns as newlines without a pty, as the cloud's universal newlines do", async () => {
    processes();
    const id = await start("printf 'a\\r\\nb\\rc\\n'");
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok.output).toBe("a\nb\nc\n");
  });

  it("counts a carriage return and a newline split across two chunks once, and a last one as a newline", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const id = await start("x");
    spawned[0]?.output("a\r");
    spawned[0]?.output("\nb\r");
    spawned[0]?.end({ code: 0, signal: null });
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok.output).toBe("a\nb\n");
  });

  it(`refuses one of two starts that come together at ${MAX_PROCESSES - 1} running`, async () => {
    processes({ runner: fake().runner });
    for (let i = 0; i < MAX_PROCESSES - 1; i += 1) await start("x");
    const answers = await Promise.all([1, 2].map(() => ask("start", { command: "x", workdir: null, task_id: "t", pty: false })));
    expect(answers.filter((answer) => answer.error?.message === TOO_MANY)).toHaveLength(1);
    expect(answers.filter((answer) => answer.ok)).toHaveLength(1);
  });

  it("keeps a flood of astral characters bounded without trimming at every chunk", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const id = await start("x");
    spawned[0]?.output("😀".repeat(200_000));
    const begun = Date.now();
    for (let i = 0; i < 2_000; i += 1) spawned[0]?.output("😀");
    expect(Date.now() - begun).toBeLessThan(250);
    expect((await ask("poll", { session_id: id })).ok.output_preview).toBe("😀".repeat(1000));
  });

  it("answers a start cancelled while it waits for the runner at once", async () => {
    processes({ runner: () => new Promise(() => {}) });
    const controller = new AbortController();
    const starting = ask("start", { command: "x", workdir: null, task_id: "t", pty: false }, controller.signal);
    await new Promise((resolve) => setTimeout(resolve, 10));
    controller.abort();
    expect(await starting).toEqual(CANCELLED);
  });

  it("answers a command that could not be spawned with its message", async () => {
    processes({ runner: fake({ fails: "spawn E2BIG" }).runner });
    expect(await ask("start", { command: "x", workdir: null, task_id: "t", pty: false })).toEqual({
      error: { type: "other", message: "spawn E2BIG" },
    });
  });
});

describe("processes from before the app quit", { timeout: 20_000 }, () => {
  it("are answered as ended until 30 minutes after they started, then forgotten", async () => {
    const now = 1_000_000;
    const handle = (id: string, started_at: number): ProcessHandle => ({ id, command: "sleep 1", cwd: base, task_id: "t", started_at });
    processes({ now: () => now, ended: [handle("proc_aaaaaaaaaaaa", now - 10), handle("proc_bbbbbbbbbbbb", now - 1801)] });
    expect((await ask("poll", { session_id: "proc_aaaaaaaaaaaa" })).ok).toEqual({
      session_id: "proc_aaaaaaaaaaaa", command: "sleep 1", status: "exited", pid: null, uptime_seconds: 10,
      output_preview: "", exit_code: null, note: APP_QUIT,
    });
    expect((await ask("poll", { session_id: "proc_bbbbbbbbbbbb" })).ok.status).toBe("not_found");
  });

  it("are kept with every process started since, each time one starts", async () => {
    const saves: string[][] = [];
    const old: ProcessHandle = { id: "proc_aaaaaaaaaaaa", command: "x", cwd: base, task_id: "t", started_at: Date.now() / 1000 };
    processes({ ended: [old], save: (handles) => saves.push(handles.map((handle) => handle.id)) });
    const id = await start("true");
    // The first save is the start's; the process's end saves again.
    expect(saves[0]).toEqual([id, old.id]);
  });

  it("keep 2 000 characters of a command and a task id, which the process's own answers keep whole", async () => {
    const { runner: driven } = fake();
    const saves: ProcessHandle[][] = [];
    processes({ runner: driven, save: (handles) => saves.push(handles) });
    // Astral, two UTF-16 units a character: the cut counts code points, as Python does.
    const long = "😀".repeat(3000);
    const id = await start(`: ${long}`, { task_id: long });
    const [kept] = saves[0] ?? [];
    expect([Array.from(kept?.command ?? "").length, Array.from(kept?.task_id ?? "").length]).toEqual([2000, 2000]);
    expect(kept?.command.startsWith(": 😀")).toBe(true);
    expect((await ask("poll", { session_id: id })).ok.command).toBe(`: ${long}`);
  });

  it("are written once when many processes end together, as when their runner dies", async () => {
    const { runner: driven, spawned } = fake();
    const saves: ProcessHandle[][] = [];
    processes({ runner: driven, save: (handles) => saves.push(handles) });
    for (let i = 0; i < 20; i += 1) await start("x");
    expect(saves).toHaveLength(20);
    for (const child of spawned) child.end({ lost: true });
    await Promise.resolve();
    expect(saves).toHaveLength(21);
    expect(saves[20]?.every((handle) => handle.ended?.note === RUNNER_GONE)).toBe(true);
  });
});

describe("a registry's output together", () => {
  // The whole output of a process whose output is one line.
  const kept = async (session_id: string) => ((await ask("read_output", { session_id, offset: 0, limit: 1 })).ok.output as string).length;

  it("stays within its bound: finished processes' output, the earliest ended first, then the longest running one's, keep their last 2 000 characters", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven, keep: 10_000 });
    const ids = [await start("a"), await start("b"), await start("c")];
    spawned[0]?.output("a".repeat(6_000));
    spawned[0]?.end({ code: 0, signal: null });
    spawned[1]?.output("b".repeat(3_000));
    expect([await kept(ids[0]!), await kept(ids[1]!)]).toEqual([6_000, 3_000]);
    spawned[2]?.output("c".repeat(1_500));
    expect(await Promise.all(ids.map((id) => kept(id)))).toEqual([2_000, 3_000, 1_500]);
    spawned[1]?.output("b".repeat(6_000));
    expect(await Promise.all(ids.map((id) => kept(id)))).toEqual([2_000, 2_000, 1_500]);
  });

  it("keeps the whole of what each shows, 200 000 characters, for as many processes as its bound holds", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven, keep: 2_000_000 });
    const ids: string[] = [];
    for (let n = 0; n < 10; n += 1) ids.push(await start(`p${n}`));
    // In three writes each: a buffer holds up to twice what it shows between its own cuts.
    for (const child of spawned) for (let write = 0; write < 3; write += 1) child.output("x".repeat(150_000));
    expect(await Promise.all(ids.map((id) => kept(id)))).toEqual(Array.from({ length: 10 }, () => 200_000));
  });

  it.each([["one-byte", "x", 1], ["two-byte", "─", 2]] as const)(
    "holds no more of the heap than its bound, for %s output: a cut keeps nothing of the string it was cut from",
    async (_kind, char, bytes) => {
      // The collector, in a context made once the flag is set.
      setFlagsFromString("--expose-gc");
      const gc = runInNewContext("gc") as () => void;
      const heap = () => {
        gc();
        return process.memoryUsage().heapUsed;
      };
      const { runner: driven, spawned } = fake();
      processes({ runner: driven, keep: 2_000_000 });
      for (let n = 0; n < MAX_PROCESSES; n += 1) await start(`p${n}`);
      const before = heap();
      // About 390 000 characters each, in 64 KiB chunks, then its end.
      const chunk = char.repeat(65_536);
      for (const child of spawned) {
        for (let write = 0; write < 6; write += 1) child.output(chunk);
        child.end({ code: 0, signal: null });
      }
      // What the bound allows, 2M code units of one or two bytes, and 2 MB more.
      expect(heap() - before).toBeLessThan(2_000_000 * bytes + 2_000_000);
    },
  );
});

describe("a background process with a terminal", { timeout: 20_000 }, () => {
  it("gets a terminal of the cloud's size, and answers its exit code", async () => {
    processes();
    const id = await start("tty; stty size; exit 7", { pty: true });
    const answer = (await ask("wait", { session_id: id, timeout: 10 })).ok;
    expect(answer.exit_code).toBe(7);
    expect(answer.output).toMatch(/^\/dev\/pts\/\d+\r\n30 120\r\n$/);
  });

  it("answers a prompt written to its stdin", async () => {
    processes();
    const id = await start("read -p 'name? ' x; echo got:$x", { pty: true });
    await until(async () => (await ask("poll", { session_id: id })).ok.output_preview === "name? ");
    expect(await ask("write_stdin", { session_id: id, data: "alice\n" })).toEqual({ ok: { status: "ok", bytes_written: 6 } });
    // The terminal echoes what it is given, as the cloud's does.
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok.output).toBe("name? alice\r\ngot:alice\r\n");
  });

  it("falls back to pipes where the sandbox has no script", async () => {
    const bin = join(base, "bin");
    mkdirSync(bin);
    for (const name of ["bash", "tty"]) symlinkSync(`/usr/bin/${name}`, join(bin, name));
    processes({ runner: () => runner(bin) });
    const id = await start("tty", { pty: true });
    expect((await ask("wait", { session_id: id, timeout: 10 })).ok).toEqual({ status: "exited", exit_code: 1, output: "not a tty\n" });
  });
});
