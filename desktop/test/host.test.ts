import { type ChildProcess, execFileSync, fork } from "node:child_process";
import {
  chmodSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync,
} from "node:fs";
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
  stderr = "";

  // Its own process group, as forkHost makes it: srt's socat bridges are the
  // host's children and outlive a host that is killed, unless the group goes.
  constructor(cwd?: string) {
    this.child = fork(HOST, [], { cwd, detached: true, stdio: ["ignore", "inherit", "pipe", "ipc"] });
    this.child.stderr?.on("data", (chunk: Buffer) => {
      this.stderr += chunk.toString();
    });
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
// Why a host would not start: its failure message, or what it said instead.
async function refusal(harness: Harness): Promise<string> {
  const said = await harness.until((messages) =>
    messages.find((message) => message.type === "ready" || message.type === "failed"));
  return said.type === "failed" ? said.message : `it answered ${said.type}`;
}
const REFUSED = /home folder or the app's own data/;

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
    mkdirSync(join(folder, "bin"));
    writeFileSync(join(folder, "bin", "only-inside"), "#!/bin/sh\n", { mode: 0o755 });
    const PATH = `${hidden}:${join(folder, "bin")}:/usr/bin:/bin`;
    expect(findOnPath("only-outside", PATH, folder)).toBe(join(hidden, "only-outside"));
    const harness = host({ env: { ...start.env, PATH } });
    await ready(harness);
    expect(await harness.op("1", "which", { name: "only-outside" })).toEqual({ ok: false });
    // Found only through the app's PATH, so the helper has it.
    expect(await harness.op("2", "which", { name: "only-inside" })).toEqual({ ok: true });
    expect(await harness.op("3", "which", { name: "sh" })).toEqual({ ok: true });
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

  it("refuses a folder inside an app folder that is spelled with a trailing slash", async () => {
    expect(PACKAGE.endsWith("/")).toBe(true);
    expect(await refusal(host({ folder: join(PACKAGE, "dist") }))).toMatch(REFUSED);
  });

  it("refuses the sibling that a credential folder is a link to", async () => {
    const home = join(base, "home");
    mkdirSync(join(home, "dotfiles"), { recursive: true });
    symlinkSync(join(home, "dotfiles"), join(home, ".ssh"));
    expect(await refusal(host({ folder: join(home, "dotfiles"), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
  });

  it("refuses the app's data folder when it is reached through a link", async () => {
    mkdirSync(join(base, "real-data", "sub"), { recursive: true });
    symlinkSync(join(base, "real-data"), join(base, "data-link"));
    expect(await refusal(host({ folder: join(base, "real-data", "sub"), dataDir: join(base, "data-link") }))).toMatch(REFUSED);
  });

  it("refuses the home folder when HOME is a link to it", async () => {
    mkdirSync(join(base, "home-real"));
    symlinkSync(join(base, "home-real"), join(base, "home-link"));
    const env = { ...start.env, HOME: join(base, "home-link") };
    expect(await refusal(host({ folder: join(base, "home-real"), env }))).toMatch(REFUSED);
  });

  // A guard that is not there yet is still where its links lead: a later `gh auth login` would write there.
  it.each(["dotfiles/config", "dotfiles"])(
    "refuses %s, which a credential folder that does not exist yet is a link into",
    async (chosen) => {
      const home = join(base, "home");
      mkdirSync(join(home, "dotfiles", "config"), { recursive: true });
      symlinkSync(join(home, "dotfiles", "config"), join(home, ".config"));
      expect(await refusal(host({ folder: join(home, chosen), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
    },
  );

  it("refuses the folder that holds a data folder, when the data folder is not there yet and its parent is a link", async () => {
    mkdirSync(join(base, "realB"));
    symlinkSync(join(base, "realB"), join(base, "linkB"));
    expect(await refusal(host({ folder: join(base, "realB"), dataDir: join(base, "linkB", "appdata") }))).toMatch(REFUSED);
  });

  it("refuses a folder that holds the place a credential folder is linked to, when that place cannot be read", async () => {
    const home = join(base, "home");
    const locked = join(base, "x", "locked");
    mkdirSync(join(locked, "ssh"), { recursive: true });
    mkdirSync(home);
    symlinkSync(join(locked, "ssh"), join(home, ".ssh"));
    chmodSync(locked, 0o000);
    try {
      expect(await refusal(host({ folder: join(base, "x"), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
    } finally {
      chmodSync(locked, 0o700);
    }
  });

  const globbed: [string, (base: string, start: HostStart) => Partial<HostStart>][] = [
    ["a folder", (b) => ({ folder: join(b, "x[ab]") })],
    ["a temp folder", (b) => ({ tmp: join(b, "data", "t?mp", "root") })],
    ["an app folder", (b, s) => ({ appDirs: [...s.appDirs, join(b, "a*")] })],
  ];
  it.each(globbed)("refuses %s whose path srt would read as a glob", async (_name, overrides) => {
    mkdirSync(join(base, "x[ab]"));
    expect(await refusal(host(overrides(base, start)))).toMatch(
      /cannot sandbox a folder whose path holds \*, \?, \[ or \]/,
    );
  });

  it.each(["/proc", "/sys", "/dev", "/dev/shm", "/run"])("refuses %s, a system folder", async (system) => {
    expect(await refusal(host({ folder: system }))).toMatch(/system folders/);
  });

  it("wants an absolute temp folder", async () => {
    expect(await refusal(host({ tmp: "relative/tmp" }, base))).toMatch(/absolute/);
  });

  it("ignores a second start once its folder is set", async () => {
    const harness = host();
    await ready(harness);
    harness.send({ ...start, folder: "/" });
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(harness.messages.some((message) => message.type === "failed")).toBe(false);
  });

  it("goes quietly when its helper is gone before an operation reaches it", async () => {
    const harness = host();
    await ready(harness);
    const pid = harness.child.pid ?? 0;
    // Stopped, so that the operation is in its queue when it learns the helper died.
    process.kill(pid, "SIGSTOP");
    execFileSync("pkill", ["-KILL", "-P", String(pid)]);
    await new Promise((resolve) => setTimeout(resolve, 300));
    harness.send({ type: "op", id: "1", kind: "stat", args: { key: `${folder}/a.txt` } });
    process.kill(pid, "SIGCONT");
    expect(await harness.exited).toBe(1);
    expect(harness.stderr).toBe("");
  });

  it("goes when its helper dies, so the app answers what ran as interrupted", async () => {
    const harness = host();
    await ready(harness);
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid)]);
    expect(await harness.exited).toBe(1);
  });
});
