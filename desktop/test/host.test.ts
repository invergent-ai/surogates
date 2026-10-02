import { type ChildProcess, execFileSync, fork } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { findOnPath } from "../src/files/operations.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "../src/hosts/messages.js";

const HOST = fileURLToPath(new URL("../dist/hosts/host.js", import.meta.url));
const PACKAGE = fileURLToPath(new URL("..", import.meta.url));

class Harness {
  readonly messages: FromHost[] = [];
  readonly child: ChildProcess;
  readonly exited: Promise<number | null>;

  // Its own process group, as forkHost makes it: srt's socat bridges are the
  // host's children and outlive a host that is killed, unless the group goes.
  constructor(cwd?: string) {
    this.child = fork(HOST, [], { cwd, detached: true, stdio: ["ignore", "inherit", "inherit", "ipc"] });
    this.exited = new Promise((resolve) => this.child.on("exit", (code) => resolve(code)));
    this.child.on("message", (message) => this.messages.push(message as FromHost));
  }

  killGroup(): void {
    try {
      if (this.child.pid !== undefined) process.kill(-this.child.pid, "SIGKILL");
    } catch {
      // Already gone.
    }
  }

  send(message: ToHost): void {
    this.child.send(message);
  }

  async until<T>(find: (messages: FromHost[]) => T | undefined, timeoutMs = 20_000): Promise<T> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const found = find(this.messages);
      if (found !== undefined) return found;
      if (Date.now() > deadline) throw new Error(`timed out; got ${JSON.stringify(this.messages)}`);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }

  async op(id: string, kind: string, args: Record<string, unknown>): Promise<unknown> {
    this.send({ type: "op", id, kind, args });
    return this.until((messages) => {
      const result = messages.find((message) => message.type === "result" && message.id === id);
      return result?.type === "result" ? result.outcome : undefined;
    });
  }
}

let base: string;
let folder: string;
let start: HostStart;
let harnesses: Harness[];

function host(overrides: Partial<HostStart> = {}, cwd?: string): Harness {
  const harness = new Harness(cwd);
  harnesses.push(harness);
  harness.send({ ...start, ...overrides });
  return harness;
}

const ready = (harness: Harness) => harness.until((messages) => messages.find((message) => message.type === "ready"));

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "host-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  start = {
    type: "start",
    folder,
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) {
    harness.killGroup();
    await harness.exited;
  }
  rmSync(base, { recursive: true, force: true });
});

describe("a tool host", { timeout: 30_000 }, () => {
  it("answers operations from inside the folder's sandbox, and stops cleanly", async () => {
    const harness = host();
    await ready(harness);
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(await harness.op("2", "read", { key: `${folder}/a.txt`, max_bytes: null })).toEqual({
      ok: Buffer.from("alpha\n").toString("base64"),
    });
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
  });

  it("cannot see what the sandbox hides", async () => {
    const hidden = join(base, "hidden-bin");
    mkdirSync(hidden);
    writeFileSync(join(hidden, "only-outside"), "#!/bin/sh\n", { mode: 0o755 });
    const PATH = `${hidden}:/usr/bin:/bin`;
    expect(findOnPath("only-outside", PATH, folder)).toBe(join(hidden, "only-outside"));
    const harness = host({ env: { ...start.env, PATH } });
    await ready(harness);
    expect(await harness.op("1", "which", { name: "only-outside" })).toEqual({ ok: false });
    expect(await harness.op("2", "which", { name: "sh" })).toEqual({ ok: true });
  });

  it("leaves the user's folder as it was", async () => {
    const before = readdirSync(folder).sort();
    // Started in the folder: without its own chdir, srt would mount its
    // placeholders here.
    const harness = host({}, folder);
    await ready(harness);
    await harness.op("1", "stat", { key: `${folder}/a.txt` });
    expect(readdirSync(folder).sort()).toEqual(before);
  });

  it("answers folder_unavailable once the folder is moved", async () => {
    const harness = host();
    await ready(harness);
    renameSync(folder, `${folder}-moved`);
    expect(await harness.op("1", "stat", { key: `${folder}/a.txt` })).toEqual(FOLDER_UNAVAILABLE);
  });

  it("fails to start without its sandbox, and says why", async () => {
    const harness = host({ bwrapPath: "/nonexistent/bwrap" });
    const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
    expect(failed.type === "failed" && failed.message).toMatch(/bwrap/);
    expect(await harness.exited).toBe(1);
  });

  it("refuses a folder that holds the app's own data or files, or the whole system", async () => {
    for (const refused of [base, "/", PACKAGE]) {
      const harness = host({ folder: refused });
      const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
      expect(failed.type === "failed" && failed.message).toMatch(/home folder or the app's own data/);
    }
  });

  it("refuses the home folder and any folder that holds or sits in a credential folder", async () => {
    const home = join(base, "home");
    mkdirSync(join(home, ".config", "gh"), { recursive: true });
    for (const refused of [home, join(home, ".config"), join(home, ".config", "gh")]) {
      const harness = host({ folder: refused, env: { ...start.env, HOME: home } });
      const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
      expect(failed.type === "failed" && failed.message).toMatch(/home folder or the app's own data/);
    }
  });

  it("goes when its helper dies, so the app answers what ran as interrupted", async () => {
    const harness = host();
    await ready(harness);
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid)]);
    expect(await harness.exited).toBe(1);
  });
});
