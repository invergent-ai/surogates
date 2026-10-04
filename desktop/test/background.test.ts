import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { readRecord, writeRecord } from "../src/hosts/folder-record.js";
import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import type { HostStart } from "../src/hosts/messages.js";
import { APP_QUIT, FINISHED_TTL_SECONDS, RUNNER_GONE, restartNotice } from "../src/hosts/processes.js";
import { appeared } from "../src/hosts/restarts.js";
import { Harness, PACKAGE } from "./host-harness.js";

type Answer = { ok?: any; error?: { type: string; message: string } };

let base: string;
let folder: string;
let start: HostStart;
let harnesses: Harness[];
let next = 0;
const id = () => `op-${next++}`;

async function host(): Promise<Harness> {
  const harness = new Harness();
  harnesses.push(harness);
  harness.send(start);
  await harness.until((messages) => messages.find((message) => message.type === "ready"));
  return harness;
}

const ask = (harness: Harness, kind: string, args: Record<string, unknown>) => harness.op(id(), kind, args) as Promise<Answer>;
const begin = (harness: Harness, command: string) =>
  ask(harness, "start", { command, workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null });
const run = (harness: Harness, command: string, timeout = 10) => ask(harness, "run", { command, workdir: null, timeout });
const running = (pattern: string) => Number(spawnSync("pgrep", ["-fc", pattern], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean | Promise<boolean>, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "background-")));
  folder = join(base, "folder");
  mkdirSync(join(folder, ".git"), { recursive: true });
  mkdirSync(join(base, "home"));
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  start = {
    type: "start",
    folder,
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("background processes in a tool host", { timeout: 30_000 }, () => {
  it("runs one in the folder's sandbox and answers for it", async () => {
    const harness = await host();
    const started = await begin(harness, "echo hi; cat a.txt; sleep 670");
    expect(started).toEqual({ ok: { session_id: expect.stringMatching(/^proc_[0-9a-f]{12}$/), pid: expect.any(Number) } });
    const session_id = started.ok.session_id as string;
    await until(async () => (await ask(harness, "poll", { session_id })).ok.output_preview === "hi\nalpha\n");
    expect((await ask(harness, "poll", { session_id })).ok).toMatchObject({ status: "running", pid: started.ok.pid });
    expect(await ask(harness, "kill", { session_id })).toEqual({ ok: { status: "killed", session_id } });
    await until(() => running("^sleep 670$") === 0);
  });

  it("lets a later command reach a server a background process started", async () => {
    const harness = await host();
    expect((await begin(harness, "python3 -m http.server 18791 --bind 127.0.0.1")).ok).toBeDefined();
    await until(async () => (await run(harness, "curl -sS http://127.0.0.1:18791/a.txt")).ok?.output === "alpha\n", 15_000);
  });

  it("starts one runner for starts that come at once", async () => {
    const harness = await host();
    const started = await Promise.all([0, 1, 2, 3, 4].map(() => begin(harness, "readlink /proc/self/ns/net; sleep 678")));
    const namespaces = new Set<string>();
    for (const answer of started) {
      const session_id = answer.ok.session_id as string;
      await until(async () => /^net:\[\d+\]\n$/.test((await ask(harness, "poll", { session_id })).ok.output_preview));
      namespaces.add((await ask(harness, "poll", { session_id })).ok.output_preview);
    }
    // One network namespace: one runner. ($PPID cannot tell: every runner is pid 2 in its own pid namespace.)
    expect(namespaces.size).toBe(1);
  });

  it("ends a wait the session cancels, and answers what comes next", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 679")).ok.session_id as string;
    const waitId = id();
    harness.send({ type: "op", id: waitId, kind: "wait", args: { session_id, timeout: 180 } });
    await new Promise((resolve) => setTimeout(resolve, 200));
    harness.send({ type: "cancel", id: waitId });
    expect(await harness.until((messages) => {
      const result = messages.find((message) => message.type === "result" && message.id === waitId);
      return result?.type === "result" ? result.outcome : undefined;
    }, 5_000)).toMatchObject({ error: { type: "cancelled" } });
    expect((await ask(harness, "poll", { session_id })).ok.status).toBe("running");
  });

  it("keeps srt's placeholders while its runner lives, and leaves none once the host stops", async () => {
    const before = readdirSync(folder).sort();
    const harness = await host();
    await begin(harness, "sleep 671");
    expect(readdirSync(folder)).toContain(".bashrc");
    // A command in the runner is not released from srt's count when it ends.
    expect((await run(harness, "true")).ok?.returncode).toBe(0);
    expect(readdirSync(folder)).toContain(".bashrc");
    await harness.stop();
    expect(await harness.exited).toBe(0);
    expect(readdirSync(folder).sort()).toEqual(before);
    expect(readdirSync(join(folder, ".git"))).toEqual([]);
    await until(() => running("^sleep 671$") === 0);
  });

  it("ends its processes with a note when its runner dies, runs commands without it, and starts another", async () => {
    const before = readdirSync(folder).sort();
    const harness = await host();
    const first = (await begin(harness, "sleep 672")).ok.session_id as string;
    // The process's parent is the runner.
    await begin(harness, "sleep 0.2; kill -KILL $PPID");
    await until(async () => (await ask(harness, "poll", { session_id: first })).ok.status === "exited");
    expect((await ask(harness, "poll", { session_id: first })).ok).toMatchObject({ exit_code: null, note: RUNNER_GONE });
    await until(() => running("^sleep 672$") === 0);
    // srt's count is released: a command of its own leaves nothing behind.
    expect((await run(harness, "true")).ok?.returncode).toBe(0);
    expect(readdirSync(folder).sort()).toEqual(before);
    const second = (await begin(harness, "sleep 673")).ok.session_id as string;
    expect((await ask(harness, "poll", { session_id: second })).ok.status).toBe("running");
  });

  it.skipIf(process.getuid?.() === 0)("refuses a write to a process's stdin while the hook guard cannot read the folder", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 689")).ok.session_id as string;
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0o000);
    try {
      // The look after a command finds what it cannot read.
      await run(harness, "true");
      expect(await ask(harness, "write_stdin", { session_id, data: "x" })).toEqual({
        error: { type: "sandbox", message: expect.stringContaining("Blocked: the computer cannot read locked") },
      });
    } finally {
      chmodSync(locked, 0o700);
    }
  });

  it("tells the app how many background processes are alive", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 675")).ok.session_id as string;
    await ask(harness, "kill", { session_id });
    const counts = harness.messages.filter((message) => message.type === "processes").map((message) => message.type === "processes" && message.live);
    expect(counts).toEqual([1, 0]);
  });

  it("stops a hook a background process writes between commands, and says so with the next one, after the restart its repository brings", async () => {
    const harness = await host();
    // Made executable before it is put in place, so only a look can have changed it.
    await begin(
      harness,
      "sleep 0.5; git -c init.defaultBranch=main init -q sub && printf '#!/bin/sh\\n' > sub/.git/h && chmod +x sub/.git/h && mv sub/.git/h sub/.git/hooks/pre-commit; sleep 676",
    );
    const hook = join(folder, "sub", ".git", "hooks", "pre-commit");
    await until(() => existsSync(hook) && (statSync(hook).mode & 0o111) === 0, 15_000);
    expect((await run(harness, "true")).ok?.output)
      .toBe(`${HOOKS_NOTICE}sub/.git/hooks/pre-commit\n${restartNotice(appeared("sub/.git/config"))}`);
  });

  it("keeps looking while the runner lives, and stops a hook a process started after a timed look writes in a new repository", { timeout: 90_000 }, async () => {
    // 100 000 hooks that cannot run, there before the host: each look over them takes seconds,
    // and the runner, wrapped after them, restarts for none of them.
    mkdirSync(join(folder, "many", ".git", "hooks"), { recursive: true });
    spawnSync("bash", ["-c", "seq 1 100000 | xargs touch"], { cwd: join(folder, "many", ".git", "hooks") });
    const harness = await host();
    await begin(harness, "true");
    // The look the start armed comes at most 5 s after it went live, which was before its answer.
    const firstLook = Date.now() + 5_000;
    await until(async () => (await ask(harness, "list_processes", { task_id: "t" })).ok[0]?.status === "exited", 15_000);
    expect(Date.now()).toBeLessThan(firstLook);
    // The look the first start armed runs now, with no process alive.
    await new Promise((done) => setTimeout(done, firstLook + 500 - Date.now()));
    // A new repository: the runner's hooks folders are read-only inside it, this one is not until a restart.
    await begin(
      harness,
      "sleep 2; git -c init.defaultBranch=main init -q other && printf '#!/bin/sh\\n' > other/.git/h && chmod +x other/.git/h && mv other/.git/h other/.git/hooks/pre-commit; sleep 677",
    );
    const hook = join(folder, "other", ".git", "hooks", "pre-commit");
    await until(() => existsSync(hook), 20_000);
    await until(() => (statSync(hook).mode & 0o111) === 0, 30_000);
  });

  it.each([
    ["stopped", (harness: Harness) => harness.stop()],
    ["killed", async (harness: Harness) => {
      harness.killGroup();
      await harness.exited;
    }],
  ])("answers for a process a host that %s had started: it ended when the app quit", async (_how, end) => {
    const first = await host();
    const session_id = (await begin(first, "echo hi; sleep 677")).ok.session_id as string;
    await end(first);
    await until(() => running("^sleep 677$") === 0);
    const second = await host();
    expect((await ask(second, "poll", { session_id })).ok).toEqual({
      session_id, command: "echo hi; sleep 677", status: "exited", pid: null, uptime_seconds: expect.any(Number),
      output_preview: "", exit_code: null, note: APP_QUIT,
    });
    expect((await ask(second, "wait", { session_id, timeout: 5 })).ok).toEqual({ status: "exited", exit_code: null, output: "", note: APP_QUIT });
    expect((await ask(second, "read_output", { session_id, offset: 0, limit: 200 })).ok).toEqual({
      session_id, status: "exited", output: "", total_lines: 0, showing: "0 lines", note: APP_QUIT,
    });
    expect(await ask(second, "kill", { session_id })).toEqual({ ok: { status: "already_exited", exit_code: null } });
    expect(await ask(second, "write_stdin", { session_id, data: "x" })).toEqual({
      ok: { status: "already_exited", error: "Process has already finished" },
    });
    expect((await ask(second, "list_processes", { task_id: "t" })).ok).toEqual([
      expect.objectContaining({ session_id, status: "exited", exit_code: null, note: APP_QUIT }),
    ]);
  });

  it("keeps a killed host's hook baseline in its record through a start, for the next host", async () => {
    mkdirSync(join(folder, ".git", "hooks"));
    writeFileSync(join(folder, ".git", "hooks", "pre-push"), "#!/bin/sh\n", { mode: 0o755 });
    const { dev, ino } = statSync(folder);
    const record = join(start.dataDir, "folders", `${dev}-${ino}.json`);
    const first = await host();
    await until(() => readRecord(record)?.hooks != null);
    const session_id = (await begin(first, "sleep 691")).ok.session_id as string;
    // Planted once the baseline is known: not the user's.
    writeFileSync(join(folder, ".git", "hooks", "pre-commit"), "#!/bin/sh\n", { mode: 0o755 });
    first.killGroup();
    await first.exited;
    await until(() => running("^sleep 691$") === 0);
    const left = readRecord(record);
    expect(left?.hooks).toEqual(expect.objectContaining({ [join(folder, ".git", "hooks", "pre-push")]: expect.any(String) }));
    expect(left?.processes.map((handle) => handle.id)).toEqual([session_id]);
    const second = await host();
    expect((await run(second, "test -x .git/hooks/pre-commit || echo not")).ok?.output).toBe("not\n");
    expect(statSync(join(folder, ".git", "hooks", "pre-push")).mode & 0o111).not.toBe(0);
  });

  it("drops a handle older than the cloud keeps one from the folder's record", async () => {
    const { dev, ino } = statSync(folder);
    const record = join(start.dataDir, "folders", `${dev}-${ino}.json`);
    const old = {
      id: "proc_000000000001", command: "echo old", cwd: folder, task_id: "t", started_at: Date.now() / 1000 - FINISHED_TTL_SECONDS - 60,
      ended: { exit_code: 0, output: "old\n", note: null },
    };
    writeRecord(record, { state: "stopped", present: [], hooks: null, processes: [old] });
    const harness = await host();
    await harness.stop();
    expect(await harness.exited).toBe(0);
    expect(readRecord(record)?.processes).toEqual([]);
  });

  it("answers how a process that ended before its host idled out ended, to the next host", async () => {
    const first = await host();
    const session_id = (await begin(first, "echo hi; exit 3")).ok.session_id as string;
    expect((await ask(first, "wait", { session_id, timeout: 10 })).ok).toEqual({ status: "exited", exit_code: 3, output: "hi\n" });
    await first.stop();
    const second = await host();
    expect((await ask(second, "wait", { session_id, timeout: 5 })).ok).toEqual({ status: "exited", exit_code: 3, output: "hi\n" });
  });
});
