import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import type { HostStart } from "../src/hosts/messages.js";
import { RUNNER_GONE } from "../src/hosts/processes.js";
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

  it("tells the app how many background processes are alive", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 675")).ok.session_id as string;
    await ask(harness, "kill", { session_id });
    const counts = harness.messages.filter((message) => message.type === "processes").map((message) => message.type === "processes" && message.live);
    expect(counts).toEqual([1, 0]);
  });

  it("stops a hook a background process writes between commands, and says so with the next one", async () => {
    const harness = await host();
    // Made executable before it is put in place, so only a look can have changed it.
    await begin(
      harness,
      "sleep 0.5; git -c init.defaultBranch=main init -q sub && printf '#!/bin/sh\\n' > sub/.git/h && chmod +x sub/.git/h && mv sub/.git/h sub/.git/hooks/pre-commit; sleep 676",
    );
    const hook = join(folder, "sub", ".git", "hooks", "pre-commit");
    await until(() => existsSync(hook) && (statSync(hook).mode & 0o111) === 0, 15_000);
    expect((await run(harness, "true")).ok?.output).toBe(`${HOOKS_NOTICE}sub/.git/hooks/pre-commit`);
  });

  it("watches a process started while the last timed look runs", { timeout: 90_000 }, async () => {
    const harness = await host();
    // 100 000 hooks that cannot run, none the user's: each look over them takes seconds.
    await begin(harness, "mkdir -p many/.git/hooks && cd many/.git/hooks && seq 1 100000 | xargs touch");
    const firstLook = Date.now() + 5_000;
    await until(async () => (await ask(harness, "list_processes", { task_id: "t" })).ok[0]?.status === "exited", 15_000);
    // The look the first start armed runs now, with nothing alive.
    await new Promise((done) => setTimeout(done, firstLook + 500 - Date.now()));
    await begin(
      harness,
      "sleep 9; printf '#!/bin/sh\\n' > many/.git/h && chmod +x many/.git/h && mv many/.git/h many/.git/hooks/pre-commit; sleep 677",
    );
    const hook = join(folder, "many", ".git", "hooks", "pre-commit");
    await until(() => existsSync(hook), 20_000);
    await until(() => (statSync(hook).mode & 0o111) === 0, 30_000);
  });
});
