import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
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
    expect(await op("start", { command: "true" })).toEqual({ error: { type: "unsupported", message: "This computer cannot do 'start' yet" } });
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
    await roots.teardown("root-1");
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(lost).toEqual([]);
    expect(await op("which", { name: "sh" }, undefined, "op-16")).toEqual(NOT_SET_UP);
    // Nothing set up: nothing to end.
    await roots.teardown("root-1");
    await roots.setup("root-1", base, R1, user);
    expect(await op("which", { name: "sh" }, undefined, "op-17")).toEqual({ ok: true });
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
    await own.teardown("root-2");
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
  it("is the cloud's layout under the root's own HOME, with the user's names", () => {
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
      HOME: "/home/ana",
      USER: "ana",
      LOGNAME: "ana",
      LANG: "C.UTF-8",
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
