import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { type ProcessHandle, RUNNER_GONE } from "../src/guest/processes.js";
import type { HostUser, Share } from "../src/guest/protocol.js";
import { enter, NOT_SET_UP, rootEnvironment, Roots } from "../src/guest/root.js";

const RUNNER = fileURLToPath(new URL("../dist/guest/runner.js", import.meta.url));
const R1: Share = { kind: "virtiofs", tag: "r1" };

let base: string;
let user: HostUser;
let children: ChildProcess[];
let roots: Roots;
// The roots the host was told it lost.
let lost: string[];

// The root runner without its namespaces, as the tests start it: the protocol is the same.
function bare(): ChildProcess {
  const child = spawn(process.execPath, [RUNNER], { cwd: base, env: { PATH: "/usr/bin:/bin", HOME: base }, stdio: ["pipe", "pipe", "pipe"] });
  children.push(child);
  return child;
}

// Kills the latest runner of a test's own: the end of a runner a test left still kills by this.
const killLatest = (own: ChildProcess[]) => () => void own.at(-1)?.kill("SIGKILL");

// A runner that says it is ready, then answers nothing, as one whose view of the folder stalls.
const STALLED = `process.stdout.write('{"ready":true}\\n'); setInterval(() => {}, 1000);`;

const op = (kind: string, args: Record<string, unknown>, signal = new AbortController().signal, id = "op-1") =>
  roots.perform("root-1", kind, args, signal, id);

async function until(check: () => boolean | Promise<boolean>, ms = 10_000): Promise<void> {
  for (const end = Date.now() + ms; !(await check()); await new Promise((resolve) => setTimeout(resolve, 50))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}

// *answer*, or "no answer" once *ms* pass.
const within = <T>(answer: Promise<T>, ms: number) =>
  Promise.race([answer, new Promise<"no answer">((resolve) => setTimeout(() => resolve("no answer"), ms))]);

beforeEach(async () => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "guest-root-")));
  user = { uid: 1000, gid: 1000, name: "someone", home: base };
  children = [];
  // This test's own: a runner an earlier test's end killed tells, and kills, its own.
  const told: string[] = [];
  const own = children;
  lost = told;
  roots = new Roots({ start: bare, uid: () => 10_000, kill: () => void own[0]?.kill("SIGKILL"), lost: (root) => told.push(root) });
  await roots.setup("root-1", base, R1, user);
});

afterEach(() => {
  for (const child of children) child.kill("SIGKILL");
  rmSync(base, { recursive: true, force: true });
});

describe("a root's commands in its runner", { timeout: 20_000 }, () => {
  it("runs a command in the folder and answers as run does", async () => {
    expect(await op("run", { command: "pwd; echo err >&2; exit 3", workdir: null, timeout: 10 })).toEqual({
      ok: { output: `${base}\n\nerr\n`, returncode: 3, timed_out: false },
    });
    // A shell reports a command a signal ended as 128 + N.
    expect(await op("run", { command: "kill -9 $$", workdir: null, timeout: 10 })).toEqual({ ok: { output: "", returncode: 137, timed_out: false } });
    mkdirSync(join(base, "sub"));
    expect(await op("run", { command: "pwd", workdir: "sub", timeout: 10 })).toEqual({
      ok: { output: `${join(base, "sub")}\n`, returncode: 0, timed_out: false },
    });
  });

  it("checks run's arguments and workdir as the cloud does", async () => {
    expect(await op("run", { command: 1, workdir: null, timeout: 10 })).toEqual({ error: { type: "value", message: "'command' must be a string" } });
    expect(await op("run", { command: "true", workdir: null, timeout: 0 })).toEqual({
      error: { type: "value", message: "'timeout' must be a positive number" },
    });
    expect(await op("run", { command: "a\0b", workdir: null, timeout: 10 })).toEqual({
      ok: { output: "embedded null byte", returncode: -1, timed_out: false },
    });
    expect(await op("run", { command: "true", workdir: "/etc", timeout: 10 })).toMatchObject({
      error: { type: "sandbox", message: expect.stringMatching(/^Blocked: .*All commands must run within the workspace directory\.$/) },
    });
    writeFileSync(join(base, "file"), "");
    expect(await op("run", { command: "true", workdir: "file", timeout: 10 })).toEqual({
      ok: { output: `[Errno 20] Not a directory: '${join(base, "file")}'`, returncode: -1, timed_out: false },
    });
  });

  it("stops a command at its timeout, or when the session cancels it", async () => {
    expect(await op("run", { command: "sleep 30", workdir: null, timeout: 0.3 })).toEqual({
      ok: { output: "Command timed out after 0.3 seconds", returncode: 124, timed_out: true },
    });
    const cancel = new AbortController();
    const running = op("run", { command: "sleep 30", workdir: null, timeout: 10 }, cancel.signal, "op-2");
    setTimeout(() => cancel.abort(), 200);
    expect(await running).toEqual(CANCELLED);
  });

  it("answers a run's timeout and cancel, and a which's cancel, while its runner has not answered", async () => {
    const stalled = new Roots({
      start: () => {
        const child = spawn(process.execPath, ["-e", STALLED], { stdio: ["pipe", "pipe", "pipe"] });
        children.push(child);
        return child;
      },
      uid: () => 10_000,
      kill: killLatest(children),
    });
    await stalled.setup("root-5", base, R1, user);
    const ask = (kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string) => stalled.perform("root-5", kind, args, signal, id);
    expect(await ask("run", { command: "true", workdir: null, timeout: 0.3 }, new AbortController().signal, "op-5")).toEqual({
      ok: { output: "Command timed out after 0.3 seconds", returncode: 124, timed_out: true },
    });
    const cancel = new AbortController();
    const running = ask("run", { command: "true", workdir: null, timeout: 30 }, cancel.signal, "op-6");
    const looking = ask("which", { name: "sh" }, cancel.signal, "op-7");
    setTimeout(() => cancel.abort(), 100);
    expect(await running).toEqual(CANCELLED);
    expect(await looking).toEqual(CANCELLED);
  });

  // A runner whose loop blocks on a stalled stat, or that a command stopped, reports no command's end.
  it("answers a run's timeout while its runner is stopped, at the timeout and its grace", async () => {
    const runner = children[0] as ChildProcess;
    const running = op("run", { command: "sleep 5", workdir: null, timeout: 0.5 }, undefined, "op-8");
    await new Promise((resolve) => setTimeout(resolve, 200));
    runner.kill("SIGSTOP");
    // 0.5 s, then 2 s of grace.
    expect(await within(running, 3_000)).toEqual({ ok: { output: "Command timed out after 0.5 seconds", returncode: 124, timed_out: true } });
    // Back, it reports the command's end, which nothing waits for any more.
    runner.kill("SIGCONT");
    expect(await op("which", { name: "sh" }, undefined, "op-9")).toEqual({ ok: true });
  });

  it("answers a run's cancel at once while its runner is stopped", async () => {
    const runner = children[0] as ChildProcess;
    const cancel = new AbortController();
    const running = op("run", { command: "sleep 5", workdir: null, timeout: 30 }, cancel.signal, "op-10");
    await new Promise((resolve) => setTimeout(resolve, 200));
    runner.kill("SIGSTOP");
    cancel.abort();
    expect(await within(running, 500)).toEqual(CANCELLED);
    runner.kill("SIGCONT");
    expect(await op("which", { name: "sh" }, undefined, "op-11")).toEqual({ ok: true });
  });

  it("answers which from the commands' PATH", async () => {
    expect(await op("which", { name: "sh" })).toEqual({ ok: true });
    expect(await op("which", { name: "no-such-tool" })).toEqual({ ok: false });
    expect(await op("which", { name: 7 })).toEqual({ error: { type: "value", message: "'name' must be a string" } });
  });

  it("answers for a root it has not set up, a kind it does not do, and a root whose runner went", async () => {
    expect(await roots.perform("root-2", "run", {}, new AbortController().signal, "op-3")).toEqual(NOT_SET_UP);
    expect(await op("bogus", {})).toEqual({ error: { type: "unsupported", message: "This computer cannot do 'bogus' yet" } });
    const running = op("run", { command: "sleep 30", workdir: null, timeout: 10 });
    await new Promise((resolve) => setTimeout(resolve, 200));
    for (const child of children) child.kill("SIGKILL");
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await op("which", { name: "sh" })).toEqual(NOT_SET_UP);
  });

  it("refuses to set up a root twice, at once too, and says why a runner did not start", async () => {
    const told: string[] = [];
    await expect(roots.setup("root-1", base, R1, user)).rejects.toThrow("This chat's sandbox is already set up");
    const both = await Promise.allSettled([roots.setup("root-2", base, R1, user), roots.setup("root-2", base, R1, user)]);
    expect(both.map((settled) => settled.status).sort()).toEqual(["fulfilled", "rejected"]);
    expect(children).toHaveLength(2);
    const broken = new Roots({
      start: () => spawn("sh", ["-c", "echo no namespaces >&2; exit 1"], { stdio: ["pipe", "pipe", "pipe"] }), uid: () => 10_000, kill: () => {},
      lost: (root) => told.push(root),
    });
    await expect(broken.setup("root-3", base, R1, user)).rejects.toThrow("no namespaces");
    expect(await broken.perform("root-3", "which", { name: "sh" }, new AbortController().signal, "op-4")).toEqual(NOT_SET_UP);
    // Never set up: nothing was lost.
    expect(told).toEqual([]);
  });

  it("loses a root whose runner answers no question in time: what waits is stopped by the sandbox, its processes end, and the host is told", async () => {
    const told: string[] = [];
    const stalled = new Roots({
      start: () => {
        const child = spawn(process.execPath, ["-e", STALLED], { stdio: ["pipe", "pipe", "pipe"] });
        children.push(child);
        return child;
      },
      uid: () => 10_000,
      kill: killLatest(children),
      lost: (root) => told.push(root),
      questionMs: 300,
    });
    await stalled.setup("root-7", base, R1, user);
    const begun = performance.now();
    const looking = stalled.perform("root-7", "which", { name: "sh" }, new AbortController().signal, "op-12");
    const running = stalled.perform("root-7", "run", { command: "true", workdir: null, timeout: 30 }, new AbortController().signal, "op-13");
    expect(await looking).toEqual(SANDBOX_STOPPED);
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(performance.now() - begun).toBeLessThan(2_000);
    expect(told).toEqual(["root-7"]);
    expect(await stalled.perform("root-7", "which", { name: "sh" }, new AbortController().signal, "op-14")).toEqual(NOT_SET_UP);
    // Lost, it can be set up again.
    await stalled.setup("root-7", base, R1, user);
  });

  it("tears a root down: its work ends, no loss is told, and it can be set up again", async () => {
    const running = op("run", { command: "sleep 3", workdir: null, timeout: 10 }, undefined, "op-15");
    await new Promise((resolve) => setTimeout(resolve, 200));
    await roots.teardown("root-1", R1);
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(lost).toEqual([]);
    expect(await op("which", { name: "sh" }, undefined, "op-16")).toEqual(NOT_SET_UP);
    // Nothing set up: nothing to end.
    await roots.teardown("root-1", R1);
    await roots.setup("root-1", base, R1, user);
    expect(await op("which", { name: "sh" }, undefined, "op-17")).toEqual({ ok: true });
  });

  it("lets its share's mount go once everything of it has ended, and lets it go for a root not set up too", async () => {
    const order: string[] = [];
    const own: ChildProcess[] = [];
    const unmounting = new Roots({
      start: () => {
        const child = bare();
        own.push(child);
        child.once("exit", () => order.push("ended"));
        return child;
      },
      uid: () => 10_000, kill: () => void own.at(-1)?.kill("SIGKILL"), unmount: async (share) => void order.push(`unmount ${share.tag}`),
    });
    await unmounting.setup("root-3", base, { kind: "virtiofs", tag: "r7" }, user);
    await unmounting.teardown("root-3", { kind: "virtiofs", tag: "r7" });
    await unmounting.teardown("root-4", { kind: "virtiofs", tag: "r8" });
    expect(order).toEqual(["ended", "unmount r7", "unmount r8"]);
  });

  it("tells the host of a lost root once everything of it has ended, and of one that cannot be ended once its runner is stopped", async () => {
    const told: string[] = [];
    let empty = () => {};
    const slow = new Roots({
      start: bare,
      uid: () => 10_000,
      // Its cgroup is empty when the test says so.
      kill: () => new Promise<void>((resolve) => {
        empty = resolve;
      }),
      lost: (root) => told.push(root),
    });
    await slow.setup("root-8", base, R1, user);
    children.at(-1)?.kill("SIGKILL");
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(told).toEqual([]);
    // Something of it still runs: it is not set up again.
    await expect(slow.setup("root-8", base, R1, user)).rejects.toThrow("This chat's sandbox is already set up");
    empty();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(told).toEqual(["root-8"]);
    await slow.setup("root-8", base, R1, user);
    const unkillable = new Roots({
      start: () => {
        const child = spawn(process.execPath, ["-e", STALLED], { stdio: ["pipe", "pipe", "pipe"] });
        children.push(child);
        return child;
      },
      uid: () => 10_000,
      kill: async () => {
        throw new Error("cgroup.kill: Permission denied");
      },
      lost: (root) => told.push(root),
      questionMs: 300,
    });
    await unkillable.setup("root-9", base, R1, user);
    expect(await unkillable.perform("root-9", "which", { name: "sh" }, new AbortController().signal, "op-18")).toEqual(SANDBOX_STOPPED);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(told).toEqual(["root-8", "root-9"]);
  });

  it("tears down a root whose setup is under way once it is set up, and sets it up again after", async () => {
    const runners = new Map<string, ChildProcess>();
    const own = new Roots({
      start: (root) => {
        const child = bare();
        runners.set(root, child);
        return child;
      },
      uid: () => 10_000,
      kill: (root) => void runners.get(root)?.kill("SIGKILL"),
    });
    const setting = own.setup("root-2", base, R1, user);
    await own.teardown("root-2", R1);
    await setting;
    expect(await own.perform("root-2", "which", { name: "sh" }, new AbortController().signal, "op-19")).toEqual(NOT_SET_UP);
    await own.setup("root-2", base, R1, user);
    expect(await own.perform("root-2", "which", { name: "sh" }, new AbortController().signal, "op-20")).toEqual({ ok: true });
  });

  it("tells the host a root whose runner went by itself", async () => {
    children[0]?.kill("SIGKILL");
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(lost).toEqual(["root-1"]);
    expect(await op("which", { name: "sh" })).toEqual(NOT_SET_UP);
  });
});

describe("a root's socket to the host proxy", { timeout: 20_000 }, () => {
  it("is made with the root's uid before its namespaces, and closed with everything of the root: torn down, lost, or never started", async () => {
    const said: string[] = [];
    const own: ChildProcess[] = [];
    let fail = false;
    const withTunnels = new Roots({
      start: () => {
        said.push("start");
        if (fail) throw new Error("no runner");
        const child = bare();
        own.push(child);
        return child;
      },
      uid: () => 10_001,
      kill: () => void own.at(-1)?.kill("SIGKILL"),
      tunnels: async (root, uid) => {
        said.push(`listen ${root} ${uid}`);
        return () => void said.push(`close ${root}`);
      },
    });
    await withTunnels.setup("root-2", base, R1, user);
    await withTunnels.teardown("root-2", R1);
    fail = true;
    await expect(withTunnels.setup("root-3", base, R1, user)).rejects.toThrow("no runner");
    fail = false;
    await withTunnels.setup("root-4", base, R1, user);
    // Its runner goes by itself: the root is lost.
    own.at(-1)?.kill("SIGKILL");
    await until(() => said.includes("close root-4"));
    expect(said).toEqual([
      "listen root-2 10001", "start", "close root-2",
      "listen root-3 10001", "start", "close root-3",
      "listen root-4 10001", "start", "close root-4",
    ]);
  });

  // Roots whose kill waits for the test, which kills the root's latest runner, as a cgroup's kill
  // ends whatever runs in the root's cgroup then; and the runners they start, exiting before
  // they are ready while *exiting* says so.
  function ending() {
    const said: string[] = [];
    const own: ChildProcess[] = [];
    const state = { exiting: false, killed: () => {} };
    const ends = new Roots({
      start: () => {
        said.push("start");
        const child = state.exiting ? spawn(process.execPath, ["-e", "process.exit(1)"], { stdio: ["pipe", "pipe", "pipe"] }) : bare();
        own.push(child);
        return child;
      },
      uid: () => 10_001,
      kill: () => new Promise<void>((resolve) => {
        said.push("kill");
        state.killed = () => {
          own.at(-1)?.kill("SIGKILL");
          resolve();
        };
      }),
      // It takes a while, as a share's unmount can.
      unmount: () => new Promise<void>((resolve) => setTimeout(() => resolve(void said.push("unmount")), 50)),
      tunnels: async (root) => {
        said.push(`listen ${root}`);
        return () => void said.push(`close ${root}`);
      },
    });
    return { ends, said, state };
  }
  const which = (target: Roots, root: string) => target.perform(root, "which", { name: "sh" }, new AbortController().signal, "op-21");

  it("sets a root up again only once its teardown under way has ended, its socket, its processes and its mount, and refuses a second setup meanwhile", async () => {
    const { ends, said, state } = ending();
    await ends.setup("root-5", base, R1, user);
    const tearing = ends.teardown("root-5", R1);
    await until(() => said.includes("kill"));
    const setting = ends.setup("root-5", base, R1, user);
    await expect(ends.setup("root-5", base, R1, user)).rejects.toThrow("This chat's sandbox is already set up");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(said).toEqual(["listen root-5", "start", "kill"]);
    state.killed();
    await tearing;
    await setting;
    expect(said).toEqual(["listen root-5", "start", "kill", "close root-5", "unmount", "listen root-5", "start"]);
    expect(await which(ends, "root-5")).toEqual({ ok: true });
  });

  it("sets a root up again only once the end of a runner that went before it was ready has ended", async () => {
    const { ends, said, state } = ending();
    state.exiting = true;
    await expect(ends.setup("root-6", base, R1, user)).rejects.toThrow("the session runner exited");
    state.exiting = false;
    const setting = ends.setup("root-6", base, R1, user);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(said).toEqual(["listen root-6", "start", "kill", "close root-6"]);
    state.killed();
    await setting;
    expect(said).toEqual(["listen root-6", "start", "kill", "close root-6", "close root-6", "listen root-6", "start"]);
    expect(await which(ends, "root-6")).toEqual({ ok: true });
  });

  it("tears down a setup that waits for a teardown under way, once it is set up", async () => {
    const { ends, said, state } = ending();
    await ends.setup("root-7", base, R1, user);
    const tearing = ends.teardown("root-7", R1);
    await until(() => said.includes("kill"));
    const setting = ends.setup("root-7", base, R1, user);
    const again = ends.teardown("root-7", R1);
    state.killed();
    await tearing;
    await setting;
    await until(() => said.filter((line) => line === "kill").length === 2);
    state.killed();
    await again;
    expect(said).toEqual(["listen root-7", "start", "kill", "close root-7", "unmount", "listen root-7", "start", "kill", "close root-7", "unmount"]);
    expect(await which(ends, "root-7")).toEqual(NOT_SET_UP);
  });
});

describe("a root's background processes", { timeout: 20_000 }, () => {
  type Answer = { ok?: any; error?: { type: string; message: string } };
  const signal = () => new AbortController().signal;
  const begin = (target: Roots, root: string, command: string, extra: Record<string, unknown> = {}) => target.perform(
    root, "start", { command, workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null, ...extra }, signal(), "op-30",
  ) as Promise<Answer>;
  const ask = (target: Roots, root: string, kind: string, args: Record<string, unknown>) => target.perform(root, kind, args, signal(), "op-31") as Promise<Answer>;
  // A bare runner's processes outlive it; one in the guest goes with its root's cgroup.
  const reap = (pattern: string) => spawnSync("pkill", ["-KILL", "-f", pattern]);

  it("starts one in the root's runner, and answers for it in the cloud's shapes", async () => {
    const started = await begin(roots, "root-1", "echo hi; sleep 30");
    expect(started).toEqual({ ok: { session_id: expect.stringMatching(/^proc_[0-9a-f]{12}$/), pid: expect.any(Number) } });
    const session_id = started.ok.session_id as string;
    await until(async () => (await ask(roots, "root-1", "poll", { session_id })).ok.output_preview === "hi\n");
    expect(await ask(roots, "root-1", "kill", { session_id })).toEqual({ ok: { status: "killed", session_id } });
    expect((await ask(roots, "root-1", "list_processes", { task_id: "t" })).ok).toEqual([
      expect.objectContaining({ session_id, status: "exited", exit_code: -15 }),
    ]);
  });

  it("keeps a root's processes' output within its share of what the agent keeps for every root, 2M characters", async () => {
    // Eleven processes that each print more than one keeps: about 2.75M characters together.
    const ids: string[] = [];
    for (let n = 0; n < 11; n += 1) ids.push((await begin(roots, "root-1", "head -c 250000 /dev/zero | tr '\\0' x")).ok.session_id);
    for (const session_id of ids) await until(async () => (await ask(roots, "root-1", "poll", { session_id })).ok.status === "exited");
    const lengths: number[] = [];
    for (const session_id of ids) lengths.push(((await ask(roots, "root-1", "read_output", { session_id, offset: 0, limit: 1 })).ok.output as string).length);
    expect(lengths.reduce((sum, length) => sum + length, 0)).toBeLessThanOrEqual(2_000_000);
    expect(lengths).toContain(200_000);
    expect(lengths).toContain(2_000);
  });

  it("sets up no ninth root while eight keep their processes, and one once a root is torn down", async () => {
    const runners = new Map<string, ChildProcess>();
    const eight = new Roots({ start: (root) => {
      const child = bare();
      runners.set(root, child);
      return child;
    }, uid: () => 10_000, kill: (root) => void runners.get(root)?.kill("SIGKILL") });
    for (let n = 1; n <= 8; n += 1) await eight.setup(`root-${n}`, base, R1, user);
    await expect(eight.setup("root-9", base, R1, user)).rejects.toThrow("This computer's sandbox holds 8 chats already");
    await eight.teardown("root-8", R1);
    await eight.setup("root-9", base, R1, user);
  });

  it("asks the runner where a start's command runs, and answers as the cloud does where it cannot", async () => {
    writeFileSync(join(base, "file"), "");
    expect(await begin(roots, "root-1", "true", { workdir: "/etc" })).toMatchObject({
      error: { type: "sandbox", message: expect.stringMatching(/^Blocked: .*All commands must run within the workspace directory\.$/) },
    });
    expect(await begin(roots, "root-1", "true", { workdir: "nope" })).toEqual({
      error: { type: "os", code: "ENOENT", message: `No such file or directory: '${join(base, "nope")}'` },
    });
    expect(await begin(roots, "root-1", "true", { workdir: "file" })).toEqual({
      error: { type: "os", code: "ENOTDIR", message: `Not a directory: '${join(base, "file")}'` },
    });
    expect(await begin(roots, "root-1", "a\0b")).toEqual({ error: { type: "value", message: "embedded null byte" } });
  });

  it("answers a start whose runner does not say where it would run as stopped by the sandbox, and loses the root", async () => {
    const told: string[] = [];
    const stalled = new Roots({
      start: () => {
        const child = spawn(process.execPath, ["-e", STALLED], { stdio: ["pipe", "pipe", "pipe"] });
        children.push(child);
        return child;
      },
      uid: () => 10_000,
      // Its runner ends at once, the rest of its cgroup a little later.
      kill: () => new Promise<void>((resolve) => {
        children.at(-1)?.kill("SIGKILL");
        setTimeout(resolve, 300);
      }),
      lost: (root) => told.push(root),
      questionMs: 300,
    });
    await stalled.setup("root-2", base, R1, user);
    expect(await begin(stalled, "root-2", "true")).toEqual(SANDBOX_STOPPED);
    // Answered once the host has been told.
    expect(told).toEqual(["root-2"]);
  });

  it("tells the host each change of a root's processes: the handles to keep, and how many live", async () => {
    const told: Array<[string, ProcessHandle[], number]> = [];
    const watched = new Roots({ start: bare, uid: () => 10_000, kill: killLatest(children), handles: (root, handles, live) => told.push([root, handles, live]) });
    await watched.setup("root-3", base, R1, user);
    const session_id = (await begin(watched, "root-3", "echo bye; exit 3")).ok.session_id as string;
    await until(() => told.at(-1)?.[2] === 0);
    expect(told[0]).toEqual(["root-3", [expect.objectContaining({ id: session_id, command: "echo bye; exit 3", cwd: base, task_id: "t" })], 1]);
    expect(told.at(-1)).toEqual(["root-3", [expect.objectContaining({ id: session_id, ended: { exit_code: 3, output: "bye\n", note: null } })], 0]);
  });

  it("answers for the handles its first setup brought, and for what ended with a lost runner once it is set up again", async () => {
    const told: string[] = [];
    const before: ProcessHandle = {
      id: "proc_000000000001", command: "make", cwd: base, task_id: "t", started_at: Date.now() / 1000, ended: { exit_code: 2, output: "failed\n", note: null },
    };
    const own = new Roots({ start: bare, uid: () => 10_000, kill: killLatest(children), lost: (root) => told.push(root) });
    await own.setup("root-4", base, R1, user, [before]);
    expect((await ask(own, "root-4", "poll", { session_id: before.id })).ok).toMatchObject({ status: "exited", exit_code: 2, output_preview: "failed\n" });
    try {
      const session_id = (await begin(own, "root-4", "sleep 694")).ok.session_id as string;
      children.at(-1)?.kill("SIGKILL");
      await until(() => told.length === 1);
      // Brought again, the host's handles are older than what the root holds.
      await own.setup("root-4", base, R1, user, []);
      expect((await ask(own, "root-4", "poll", { session_id })).ok).toMatchObject({ status: "exited", exit_code: null, note: RUNNER_GONE });
      expect((await ask(own, "root-4", "poll", { session_id: before.id })).ok).toMatchObject({ exit_code: 2 });
    } finally {
      reap("^sleep 694$");
    }
  });

  it("answers a process its setup brought as still running as one that ended with its sandbox: the guest it ran in went", async () => {
    const running: ProcessHandle = { id: "proc_000000000002", command: "npm run dev", cwd: base, task_id: "t", started_at: Date.now() / 1000 };
    const own = new Roots({ start: bare, uid: () => 10_000, kill: killLatest(children) });
    await own.setup("root-6", base, R1, user, [running]);
    expect((await ask(own, "root-6", "poll", { session_id: running.id })).ok).toMatchObject({ status: "exited", exit_code: null, note: RUNNER_GONE });
  });

  it("tells the host nothing more of a root it tears down, and forgets its processes", async () => {
    const told: number[] = [];
    const own = new Roots({ start: bare, uid: () => 10_000, kill: killLatest(children), handles: (_root, _handles, live) => told.push(live) });
    await own.setup("root-5", base, R1, user);
    try {
      const session_id = (await begin(own, "root-5", "sleep 695")).ok.session_id as string;
      await until(() => told.at(-1) === 1);
      await own.teardown("root-5", R1);
      await new Promise((resolve) => setTimeout(resolve, 200));
      // What the teardown ended is not told: the host keeps the process as it started, and it ended as the app quit.
      expect(told).toEqual([1]);
      await own.setup("root-5", base, R1, user);
      expect((await ask(own, "root-5", "poll", { session_id })).ok.status).toBe("not_found");
    } finally {
      reap("^sleep 695$");
    }
  });
});

describe("a root's inputs", () => {
  const place = { folder: "/home/ana/project", home: "/home/ana" };
  const ana: HostUser = { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" };

  // Each is refused before anything is made: here, where the guest's sessions disk is not, giving a uid would fail with ENOENT.
  it.each([
    ["../etc", place, "r1", ana, "not a root session id: ../etc"],
    ["root-1", place, "../r1", ana, "not a share tag: ../r1"],
    ["root-1", place, "r1", { ...ana, name: "ana:x" }, "not a user name: ana:x"],
    ["root-1", { ...place, folder: "project" }, "r1", ana, "not a folder: project"],
    ["root-1", { ...place, folder: "/home/ana/project/" }, "r1", ana, "not a folder: /home/ana/project/"],
    ["root-1", { ...place, folder: "/home/ana/a\nb" }, "r1", ana, "not a folder: /home/ana/a\nb"],
    ["root-1", { ...place, home: "/home/a:na" }, "r1", ana, "not a home folder: /home/a:na"],
    ["root-1", { ...place, home: "/home/ana/" }, "r1", ana, "not a home folder: /home/ana/"],
  ])("refuses %s %j %s %j before anything is given", async (root, at, tag, user, message) => {
    await expect(enter(root, at, { kind: "virtiofs", tag }, user)).rejects.toThrow(message);
  });
});

describe("a root's environment", () => {
  it("is the cloud's layout under the root's own HOME, with the user's names and the runner's proxies", () => {
    const layout = [
      "PATH=/home/sandbox/.npm-global/bin:/home/sandbox/.local/bin:/opt/venv/bin:/usr/bin:/bin",
      "PYTHONUSERBASE=/home/sandbox/.local",
      "PIP_USER=1",
      "",
    ].join("\n");
    expect(rootEnvironment(layout, { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" })).toEqual({
      PATH: "/home/ana/.npm-global/bin:/home/ana/.local/bin:/opt/venv/bin:/usr/bin:/bin",
      PYTHONUSERBASE: "/home/ana/.local",
      PIP_USER: "1",
      HTTP_PROXY: "http://127.0.0.1:3128",
      HTTPS_PROXY: "http://127.0.0.1:3128",
      ALL_PROXY: "http://127.0.0.1:3128",
      http_proxy: "http://127.0.0.1:3128",
      https_proxy: "http://127.0.0.1:3128",
      all_proxy: "http://127.0.0.1:3128",
      NO_PROXY: "localhost,127.0.0.1,::1,0.0.0.0,surogate",
      no_proxy: "localhost,127.0.0.1,::1,0.0.0.0,surogate",
      HOME: "/home/ana",
      USER: "ana",
      LOGNAME: "ana",
      LANG: "C.UTF-8",
      GIT_CONFIG_COUNT: "1",
      GIT_CONFIG_KEY_0: "core.checkStat",
      GIT_CONFIG_VALUE_0: "minimal",
    });
  });

  it("gives git its stat check after the layout's own git config", () => {
    const layout = "GIT_CONFIG_COUNT=1\nGIT_CONFIG_KEY_0=safe.directory\nGIT_CONFIG_VALUE_0=*\n";
    expect(rootEnvironment(layout, { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" })).toMatchObject({
      GIT_CONFIG_COUNT: "2",
      GIT_CONFIG_KEY_0: "safe.directory",
      GIT_CONFIG_VALUE_0: "*",
      GIT_CONFIG_KEY_1: "core.checkStat",
      GIT_CONFIG_VALUE_1: "minimal",
    });
  });

  it("keeps the runner's proxies whatever the layout says", () => {
    expect(rootEnvironment("HTTPS_PROXY=http://elsewhere:8080\nNO_PROXY=*\n", { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" })).toMatchObject({
      HTTPS_PROXY: "http://127.0.0.1:3128", NO_PROXY: "localhost,127.0.0.1,::1,0.0.0.0,surogate",
    });
  });

  it("takes the home as it is, and moves only the paths that start at the cloud's HOME", () => {
    const layout = "PATH=/home/sandbox/.local/bin:/opt/home/sandbox/bin:/home/sandboxes/bin\nPYTHONUSERBASE=/home/sandbox\n";
    const home = "/home/a$&b$$c$`d$'e";
    expect(rootEnvironment(layout, { uid: 1000, gid: 1000, name: "ana", home })).toMatchObject({
      PATH: `${home}/.local/bin:/opt/home/sandbox/bin:/home/sandboxes/bin`,
      PYTHONUSERBASE: home,
    });
  });
});
