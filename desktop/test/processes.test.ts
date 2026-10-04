import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OUTPUT_CAP_CHARS, pyJsonLength, sandboxError } from "../src/files/answers.js";
import type { Outcome } from "../src/link/protocol.js";
import {
  APP_QUIT, MAX_PROCESSES, type ProcessHandle, Processes, type ProcessesOptions, RESTARTED, RUNNER_GONE, type Spawner, TOO_MANY,
  restartNotice,
} from "../src/hosts/processes.js";
import { CANCELLED, type CommandEnd } from "../src/hosts/run.js";
import { SessionRunner } from "../src/hosts/session-runner.js";

const RUNNER = fileURLToPath(new URL("../dist/hosts/runner.js", import.meta.url));

let base: string;
let runners: SessionRunner[];
let children: ChildProcess[];
let registry: Processes;

// A runner without srt, as the host would start one; PATH is what the commands find.
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
    context: { folder: base, home: base, env: {}, claudeWasAbsent: false },
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
        started: fails === undefined
          ? Promise.resolve(pid)
          : new Promise((resolve) => setTimeout(() => { end({ failed: fails }); resolve(null); }, 0)),
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
    const id = await start("setsid sleep 661 & sleep 662");
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
    expect(await answer(null, "a\0b")).toEqual({ error: { type: "value", message: "embedded null byte" } });
    // Popen refuses the NUL before it looks at the cwd.
    expect(await answer("nope", "a\0b")).toEqual({ error: { type: "value", message: "embedded null byte" } });
    // A refusal quotes the workdir as it came, lone surrogate and all.
    expect((await answer("/\ud800")).error?.message).toBe(
      `Blocked: Path traversal blocked: '/\ufffd' resolves to '/\ufffd' which is outside the workspace '${base}'. All commands must run within the workspace directory.`,
    );
    expect((await answer("/etc", "a\0b")).error?.type).toBe("sandbox");
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

  it("answers a command that could not be spawned with its message", async () => {
    processes({ runner: fake({ fails: "spawn E2BIG" }).runner });
    expect(await ask("start", { command: "x", workdir: null, task_id: "t", pty: false })).toEqual({
      error: { type: "other", message: "spawn E2BIG" },
    });
  });
});

describe("a runner restart", () => {
  const notice = restartNotice("a folder grant changed");

  it("ends every live process with a note of its own, however it goes, and keeps it for the next host", async () => {
    const { runner: driven, spawned } = fake();
    const saves: ProcessHandle[][] = [];
    processes({ runner: driven, save: (handles) => saves.push(handles) });
    const first = await start("x");
    const second = await start("y");
    expect(registry.restart("a folder grant changed")).toBe(2);
    spawned[0]?.end({ lost: true });
    spawned[1]?.end({ code: null, signal: "SIGKILL" });
    for (const id of [first, second]) {
      expect((await ask("poll", { session_id: id })).ok).toMatchObject({ status: "exited", exit_code: null, note: RESTARTED });
    }
    await Promise.resolve();
    expect(saves.at(-1)?.map((handle) => handle.ended?.note)).toEqual([RESTARTED, RESTARTED]);
  });

  it("tells the next answer that has no note of its own, once", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const id = await start("x");
    registry.restart("a folder grant changed");
    spawned[0]?.end({ lost: true });
    // The process's own note says it was restarted: the notice waits.
    expect((await ask("wait", { session_id: id, timeout: 5 })).ok.note).toBe(RESTARTED);
    expect((await ask("kill", { session_id: id })).ok).toEqual({ status: "already_exited", exit_code: null, note: notice });
    expect((await ask("kill", { session_id: id })).ok).toEqual({ status: "already_exited", exit_code: null });
    expect(registry.takeNotice()).toBeNull();
  });

  it("puts the notice on each process the restart ended in a list, once", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const done = await start("done");
    spawned[0]?.end({ code: 0, signal: null });
    const live = await start("live");
    registry.restart("a folder grant changed");
    spawned[1]?.end({ lost: true });
    const listed = (await ask("list_processes", { task_id: "t" })).ok as Array<{ session_id: string; note?: string }>;
    expect(listed.map((entry) => [entry.session_id, entry.note])).toEqual([[done, undefined], [live, notice]]);
    const again = (await ask("list_processes", { task_id: "t" })).ok as Array<{ session_id: string; note?: string }>;
    expect(again.map((entry) => entry.note)).toEqual([undefined, RESTARTED]);
  });

  it("gives the notice to a run's output once, and never to a start", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    await start("x");
    registry.restart("a folder grant changed");
    // Not before the process has ended.
    expect(registry.takeNotice()).toBeNull();
    spawned[0]?.end({ lost: true });
    expect(Object.keys((await ask("start", { command: "y", workdir: null, task_id: "t", pty: false })).ok).sort()).toEqual(["pid", "session_id"]);
    expect(registry.takeNotice()).toBe(notice);
    expect(registry.takeNotice()).toBeNull();
  });

  it("tells two restarts the agent has not heard of yet once, with the later reason, on every process they ended", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const first = await start("x");
    registry.restart("a folder grant changed");
    spawned[0]?.end({ lost: true });
    const second = await start("y");
    registry.restart("a protected file appeared in the folder (.git/config)");
    spawned[1]?.end({ lost: true });
    const later = restartNotice("a protected file appeared in the folder (.git/config)");
    const listed = (await ask("list_processes", { task_id: "t" })).ok as Array<{ session_id: string; note?: string }>;
    expect(listed.map((entry) => [entry.session_id, entry.note])).toEqual([[first, later], [second, later]]);
    expect(registry.takeNotice()).toBeNull();
  });

  it("is silent when no process was live", async () => {
    const { runner: driven, spawned } = fake();
    processes({ runner: driven });
    const id = await start("x");
    spawned[0]?.end({ code: 0, signal: null });
    expect(registry.restart("a folder grant changed")).toBe(0);
    expect((await ask("poll", { session_id: id })).ok).not.toHaveProperty("note");
    expect(registry.takeNotice()).toBeNull();
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

  it("are written once when many processes end together, as when their runner dies", async () => {
    const { runner: driven, spawned } = fake();
    const saves: ProcessHandle[][] = [];
    processes({ runner: driven, save: (handles) => saves.push(handles) });
    for (let i = 0; i < 20; i += 1) await start("x");
    expect(saves).toHaveLength(20);
    for (const child of spawned) child.end({ lost: true });
    await Promise.resolve();
    expect(saves).toHaveLength(21);
    expect(saves[20]?.every((handle) => handle.ended === undefined)).toBe(true);
  });
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
