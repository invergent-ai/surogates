import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import type { HostUser } from "../src/guest/protocol.js";
import { NOT_SET_UP, rootEnvironment, Roots } from "../src/guest/root.js";

const RUNNER = fileURLToPath(new URL("../dist/guest/runner.js", import.meta.url));

let base: string;
let user: HostUser;
let children: ChildProcess[];
let roots: Roots;

// The root runner without its namespaces, as the tests start it: the protocol is the same.
function bare(): ChildProcess {
  const child = spawn(process.execPath, [RUNNER], { cwd: base, env: { PATH: "/usr/bin:/bin", HOME: base }, stdio: ["pipe", "pipe", "pipe"] });
  children.push(child);
  return child;
}

// A runner that says it is ready, then answers nothing, as one whose view of the folder stalls.
const STALLED = `process.stdout.write('{"ready":true}\\n'); setInterval(() => {}, 1000);`;

const op = (kind: string, args: Record<string, unknown>, signal = new AbortController().signal, id = "op-1") =>
  roots.perform("root-1", kind, args, signal, id);

beforeEach(async () => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "guest-root-")));
  user = { uid: 1000, gid: 1000, name: "someone", home: base };
  children = [];
  roots = new Roots({ start: bare, uid: () => 10_000 });
  await roots.setup("root-1", base, "r1", user);
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
    });
    await stalled.setup("root-5", base, "r1", user);
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
    await expect(roots.setup("root-1", base, "r1", user)).rejects.toThrow("This chat's sandbox is already set up");
    const both = await Promise.allSettled([roots.setup("root-2", base, "r1", user), roots.setup("root-2", base, "r1", user)]);
    expect(both.map((settled) => settled.status).sort()).toEqual(["fulfilled", "rejected"]);
    expect(children).toHaveLength(2);
    // Nothing is given before the root's inputs are checked: not even its uid.
    const unchecked = new Roots({
      start: () => {
        throw new Error("not a share tag: ../r1");
      },
      uid: () => {
        throw new Error("asked for a uid");
      },
    });
    await expect(unchecked.setup("root-6", base, "../r1", user)).rejects.toThrow("not a share tag: ../r1");
    const broken = new Roots({ start: () => spawn("sh", ["-c", "echo no namespaces >&2; exit 1"], { stdio: ["pipe", "pipe", "pipe"] }), uid: () => 10_000 });
    await expect(broken.setup("root-3", base, "r1", user)).rejects.toThrow("no namespaces");
    expect(await broken.perform("root-3", "which", { name: "sh" }, new AbortController().signal, "op-4")).toEqual(NOT_SET_UP);
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
});
