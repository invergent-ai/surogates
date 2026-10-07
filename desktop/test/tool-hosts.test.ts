import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { perform } from "../src/files/operations.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { FOLDER_UNAVAILABLE, type FromHost, type NetworkAnswer, type NetworkAsk, type ToHost } from "../src/hosts/messages.js";
import {
  APP_DIRS, CANCELLED, forkHost, HOST_STOPPED, type HostProcess, NODE, NOT_BOUND, START_TIMEOUT_MS, ToolHosts,
  type ToolHostsOptions,
} from "../src/hosts/tool-hosts.js";

const ROOT_A = "11111111-1111-4111-8111-111111111111";
const ROOT_B = "22222222-2222-4222-8222-222222222222";
const SLEEP = "37.25";
// What a search asks for when it is to stay running: only then does the fake rg sleep.
const NEVER = "never-answers";

// How many of the fake rg's sleeps are running, the sandbox's included.
const sleeping = () => Number(spawnSync("pgrep", ["-fc", `^sleep ${SLEEP}$`], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

let base: string;
let folders: Record<string, string>;
// Each folder's identity as it was made: the one its binding holds.
let identities: Map<string, { dev: number; ino: number; boot: string }>;
let spawned: HostProcess[];
let exits: number;
let hosts: ToolHosts | null;
// This process's PATH, which each test puts the fake rg's folder before, as a host forks with it.
let ownPath: string | undefined;

const op = (kind: string, args: Record<string, unknown>, sessionId = ROOT_A): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId, callingSessionId: sessionId, invocationId: "call", ordinal: 1, kind, args, digest: "d",
});

function toolHosts(overrides: Partial<ToolHostsOptions> = {}): ToolHosts {
  hosts = new ToolHosts({
    bindingOf: (root) => {
      const folder = folders[root];
      const made = folder === undefined ? undefined : identities.get(folder);
      return folder && made ? { folder, ...made } : undefined;
    },
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8" },
    // The fake rg's folder: one of the app's, which the helper's sandbox reads.
    appDirs: [...APP_DIRS, join(base, "tools")],
    spawnHost: () => {
      const host = forkHost();
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return host;
    },
    ...overrides,
  });
  return hosts;
}

const signal = () => new AbortController().signal;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "tool-hosts-")));
  folders = { [ROOT_A]: join(base, "a"), [ROOT_B]: join(base, "b") };
  identities = new Map();
  for (const folder of Object.values(folders)) {
    mkdirSync(folder);
    const { dev, ino } = statSync(folder);
    identities.set(folder, { dev, ino, boot: BOOT_ID });
  }
  // An rg that never answers a search for NEVER, for operations that are still running, and
  // is the real one otherwise, as srt's own scan runs it. The helper finds it on the host's
  // own PATH, never in the folder. Its sleep has a length only it uses, so the test can see it run and stop.
  mkdirSync(join(base, "tools"));
  writeFileSync(join(base, "tools", "rg"), `#!/bin/sh\ncase "$*" in *${NEVER}*) exec sleep ${SLEEP};; esac\nexec /usr/bin/rg "$@"\n`, { mode: 0o755 });
  ownPath = process.env.PATH;
  process.env.PATH = `${join(base, "tools")}:${ownPath ?? ""}`;
  spawned = [];
  exits = 0;
  hosts = null;
});

afterEach(async () => {
  process.env.PATH = ownPath;
  await hosts?.stop();
  // A host that failed to start, or timed out, is no longer in hosts and is on its way out. srt
  // cleans up /tmp only when a host exits by itself, so each is given the time before any is killed.
  await until(() => exits >= spawned.length, 3_000).catch(() => {});
  for (const host of spawned) host.kill();
  rmSync(base, { recursive: true, force: true });
});

const slowSearch = () => op("ripgrep", { key: folders[ROOT_A], mode: "files", pattern: NEVER, glob: null, context: 0 });

describe("a command elsewhere (the VM), under the hook guard", { timeout: 30_000 }, () => {
  it("is refused before it runs while a link at a protected name leads into the folder, and not for one that leads out of it", async () => {
    const a = folders[ROOT_A] ?? "";
    // An editor's settings shared with a sibling worktree, out of the folder.
    mkdirSync(join(base, "shared-vscode"));
    mkdirSync(join(a, "sub"));
    symlinkSync(join(base, "shared-vscode"), join(a, "sub", ".vscode"));
    writeFileSync(join(a, "mcp.json"), "{}\n");
    symlinkSync("mcp.json", join(a, ".mcp.json"));
    symlinkSync("missing", join(a, ".idea"));
    symlinkSync(join(base, "nowhere"), join(a, ".zshrc"));
    let ran = 0;
    const outcome = await toolHosts().guarded(op("run", { command: "true" }), signal(), "around", async () => {
      ran += 1;
      return { ok: { output: "", returncode: 0, timed_out: false } };
    });
    expect([outcome, ran]).toEqual([{
      error: {
        type: "sandbox",
        message: "Blocked: .idea is a link to missing in this folder. .mcp.json is a link to mcp.json in this folder. Make each a file or folder of its own, or point it outside the folder or at a protected name, to run commands here.",
      },
    }, 0]);
  });
});

describe("ToolHosts", { timeout: 30_000 }, () => {
  it("runs each root's operations in its own folder", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("resolve", { path: "" }, ROOT_A), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual({ ok: folders[ROOT_B] });
    expect(await executor.run(op("resolve", { path: "x" }, ROOT_A), signal())).toEqual({ ok: `${folders[ROOT_A]}/x` });
    expect(spawned).toHaveLength(2);
  });

  it("answers a root it has no folder for, and a bind it cannot confirm yet", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("stat", { key: "/x" }, "33333333-3333-4333-8333-333333333333"), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await executor.run(op("stat", { key: "/x" }, "../../etc"), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await executor.run(op("bind", { folder: "/x", nonce: "n" }), signal())).toEqual(NOT_BOUND);
    expect(spawned).toHaveLength(0);
  });

  it("answers every operation when the sandbox cannot start, and tries again for the next", async () => {
    const executor = toolHosts({ bwrapPath: "/nonexistent/bwrap" });
    const first = await executor.run(op("stat", { key: "/x" }), signal());
    expect(first).toMatchObject({ error: { type: "unavailable" } });
    expect((first as { error: { message: string } }).error.message).toMatch(/bwrap/);
    expect(await executor.run(op("stat", { key: "/x" }), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(2);
  });

  it("answers what was running as interrupted when its host dies, and starts a new host", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    const running = executor.run(slowSearch(), signal());
    await until(() => sleeping() === 1);
    spawned[0]?.kill();
    expect(await running).toEqual(HOST_STOPPED);
    await until(() => sleeping() === 0);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(spawned).toHaveLength(2);
  });

  it("answers a cancelled operation at once and stops its work, and its host goes on", async () => {
    const executor = toolHosts();
    const controller = new AbortController();
    const running = executor.run(slowSearch(), controller.signal);
    await until(() => sleeping() === 1);
    controller.abort();
    expect(await running).toEqual(CANCELLED);
    await until(() => sleeping() === 0);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(spawned).toHaveLength(1);
  });

  it("answers folder_unavailable for a folder that has gone before its host starts", async () => {
    const executor = toolHosts();
    renameSync(folders[ROOT_B] ?? "", `${folders[ROOT_B]}-moved`);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual(FOLDER_UNAVAILABLE);
    // The app stats nothing itself: the host it starts is the one that finds the folder gone, and goes.
    expect(spawned).toHaveLength(1);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(spawned).toHaveLength(2);
    // Each of them goes on its own; none is left to exit during the next test.
    await until(() => exits === 2);
  });

  it("answers folder_unavailable for a folder replaced since it was bound, before its host starts", async () => {
    const executor = toolHosts();
    renameSync(folders[ROOT_B] ?? "", `${folders[ROOT_B]}-old`);
    mkdirSync(folders[ROOT_B] ?? "");
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual(FOLDER_UNAVAILABLE);
    await until(() => exits === 1);
  });

  it("starts no host once stopped", async () => {
    const executor = toolHosts();
    await executor.stop();
    expect(await executor.run(op("resolve", { path: "" }), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(0);
  });

  it("stops every host", async () => {
    const executor = toolHosts();
    await executor.run(op("resolve", { path: "" }, ROOT_A), signal());
    await executor.run(op("resolve", { path: "" }, ROOT_B), signal());
    await executor.stop();
    expect(exits).toBe(2);
  });

  it("lets one chat at a time work in a folder, and the next once the first is idle", async () => {
    folders[ROOT_B] = folders[ROOT_A] ?? "";
    const executor = toolHosts({ idleMs: 500 });
    const busy = new AbortController();
    const first = executor.run(slowSearch(), busy.signal);
    await until(() => sleeping() === 1, 20_000);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toMatchObject({
      error: { type: "unavailable", message: expect.stringMatching(/another chat on this computer/) },
    });
    busy.abort();
    expect(await first).toEqual(CANCELLED);
    // Half a second with nothing to do, and the first chat's host lets the folder go.
    await until(() => exits >= 2, 10_000);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toMatchObject({ ok: expect.any(String) });
  }, 40_000);
});

// A host that does what the test tells it to, so the paths a real host takes only
// by failing (never speaking, dying, not starting) need no srt.
class FakeHost implements HostProcess {
  readonly sent: ToHost[] = [];
  killed = 0;
  private readonly messageListeners: Array<(message: FromHost) => void> = [];
  private readonly exitListeners: Array<() => void> = [];
  private done = false;

  constructor(private readonly behave: (host: FakeHost, message: ToHost) => void) {}

  send(message: ToHost): void {
    this.sent.push(message);
    queueMicrotask(() => this.behave(this, message));
  }

  onMessage(listener: (message: FromHost) => void): void {
    this.messageListeners.push(listener);
  }

  onExit(listener: () => void): void {
    this.exitListeners.push(listener);
  }

  kill(): void {
    this.killed += 1;
    this.exit();
  }

  say(message: FromHost): void {
    for (const listener of this.messageListeners) listener(message);
  }

  exit(): void {
    if (this.done) return;
    this.done = true;
    for (const listener of this.exitListeners) listener();
  }

  count(type: ToHost["type"]): number {
    return this.sent.filter((message) => message.type === type).length;
  }
}

// What a host does when it is stopped.
const onStop = (host: FakeHost, message: ToHost) => {
  if (message.type === "stop") host.exit();
};
// Says ready, then answers nothing: its operations stay running.
const readyOnly = (host: FakeHost, message: ToHost) => {
  if (message.type === "start") host.say({ type: "ready", processes: [] });
  onStop(host, message);
};
const answering = (host: FakeHost, message: ToHost) => {
  readyOnly(host, message);
  if (message.type === "op") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
};

// Spike Q7's race (M8): a program of this computer's, outside every sandbox, flips a folder in the
// chat's folder between a folder and a link to one beside it, while the file tools work through it.
describe("the file tools, against a process of this computer's racing them", { timeout: 120_000 }, () => {
  type Answer = (kind: string, args: Record<string, unknown>) => Promise<Outcome>;

  // 1 250 rounds of a write, a read, a listing and a delete through the flipped folder, and a search
  // of it every tenth: what of the folder beside it each reached (its file gone counts), how many
  // writes were answered, what was written beside it, and what its file holds after.
  async function race(answer: Answer, folder: string, outside: string) {
    rmSync(outside, { recursive: true, force: true });
    mkdirSync(outside);
    writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
    // It ends with this process: a run killed partway leaves no attacker spinning.
    const flipper = spawn("sh", ["-c", `cd '${folder}' && while kill -0 ${process.pid} 2>/dev/null; do rm -rf sub; mkdir sub; echo inside > sub/inside; rm -rf sub; ln -s '${outside}' sub; done`], {
      stdio: "ignore", detached: true,
    });
    let reached = 0;
    let wrote = 0;
    try {
      for (let i = 0; i < 1_250; i += 1) {
        if ("ok" in (await answer("write", { key: join(folder, "sub", `x-${i}`), data: Buffer.from("m8\n").toString("base64") }))) wrote += 1;
        const read = await answer("read", { key: join(folder, "sub", "only-outside"), max_bytes: null });
        if ("ok" in read && typeof read.ok === "string" && Buffer.from(read.ok, "base64").toString() === "OUTSIDE\n") reached += 1;
        const listed = await answer("list_dir", { key: join(folder, "sub") });
        if ("ok" in listed && Array.isArray(listed.ok) && listed.ok.includes("only-outside")) reached += 1;
        await answer("delete", { key: join(folder, "sub", "only-outside") });
        if (i % 10 === 0) {
          const found = await answer("ripgrep", { key: join(folder, "sub"), mode: "count", pattern: "OUTSIDE", glob: null, context: 0 });
          if ("ok" in found && typeof found.ok === "string" && found.ok.includes("only-outside")) reached += 1;
        }
      }
    } finally {
      process.kill(-flipper.pid!, "SIGKILL");
    }
    const left = readdirSync(outside).sort();
    const kept = left.includes("only-outside") ? readFileSync(join(outside, "only-outside"), "utf8") : null;
    return { reached: reached + (kept === null ? 1 : 0), wrote, wroteOutside: left.filter((name) => name !== "only-outside").length, kept };
  }

  it("reach nothing beside the folder through its file host on the app's own node, where the same operations outside any sandbox do", async () => {
    const folder = folders[ROOT_A]!;
    const outside = join(base, "outside");
    // The probe can race: the helper's own checks alone, outside srt, are beaten.
    const bare = await race((kind, args) => perform(kind, args, { folder, home: base, env: { PATH: "/usr/bin:/bin" } }, signal()), folder, outside);
    expect(bare.reached + bare.wroteOutside).toBeGreaterThan(0);
    const executor = toolHosts();
    const sandboxed = await race((kind, args) => executor.run(op(kind, args), signal()), folder, outside);
    console.log(`M8: ${JSON.stringify({ bare, sandboxed })}`);
    // The file host worked in the folder: its writes landed while sub was a folder.
    expect(sandboxed).toEqual({ reached: 0, wrote: expect.any(Number), wroteOutside: 0, kept: "OUTSIDE\n" });
    expect(sandboxed.wrote).toBeGreaterThan(0);
    // The attacker met the file host: some of its writes found sub a link, or gone.
    expect(sandboxed.wrote).toBeLessThan(1_250);
  });
});

describe("ToolHosts, when hosts misbehave", { timeout: 5_000 }, () => {
  let fakes: FakeHost[];
  const fakeSpawn = (behave: (host: FakeHost, message: ToHost) => void) => () => {
    const host = new FakeHost(behave);
    fakes.push(host);
    return host;
  };
  const resolve = () => op("resolve", { path: "" });
  const unavailable = { error: { type: "unavailable" } };
  const sent = (index: number, type: ToHost["type"]) => (fakes[index]?.count(type) ?? 0);

  beforeEach(() => {
    fakes = [];
  });

  it("answers unavailable and kills a host that never speaks, then starts another for the next", async () => {
    const executor = toolHosts({ startTimeoutMs: 100, spawnHost: fakeSpawn(onStop) });
    expect(await executor.run(resolve(), signal())).toMatchObject(unavailable);
    expect(fakes[0]?.killed).toBe(1);
    expect(await executor.run(resolve(), signal())).toMatchObject(unavailable);
    expect(fakes).toHaveLength(2);
  });

  it("answers folder_unavailable when its host says the folder is not there, and starts another for the next", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn((host, message) => {
      if (message.type === "start") host.say({ type: "failed", message: "not a folder", folder: true });
      onStop(host, message);
    }) });
    expect(await executor.run(resolve(), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await executor.run(resolve(), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(fakes).toHaveLength(2);
  });

  it("answers unavailable, with its reason, when its host fails for any other cause", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn((host, message) => {
      if (message.type === "start") host.say({ type: "failed", message: "no sandbox" });
      onStop(host, message);
    }) });
    expect(await executor.run(resolve(), signal())).toEqual({
      error: { type: "unavailable", message: "This computer could not open the folder's sandbox: no sandbox" },
    });
  });

  it("keeps a host that started, once its start timeout has passed", async () => {
    const executor = toolHosts({ startTimeoutMs: 100, spawnHost: fakeSpawn(answering) });
    expect(await executor.run(op("resolve", { path: "" }, ROOT_A), signal())).toMatchObject({ ok: expect.any(String) });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(fakes[0]?.killed).toBe(0);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_A), signal())).toMatchObject({ ok: expect.any(String) });
    expect(fakes).toHaveLength(1);
  });

  it("gives a host START_TIMEOUT_MS to start by default", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const executor = toolHosts({ spawnHost: fakeSpawn(onStop) });
      let answer: unknown = null;
      void executor.run(resolve(), signal()).then((outcome) => {
        answer = outcome;
      });
      await vi.advanceTimersByTimeAsync(START_TIMEOUT_MS - 1);
      expect(answer).toBeNull();
      await vi.advanceTimersByTimeAsync(1);
      expect(answer).toMatchObject(unavailable);
      expect(fakes[0]?.killed).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("answers an operation interrupted when its host exits after it was ready", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(readyOnly) });
    const running = executor.run(resolve(), signal());
    await until(() => sent(0, "op") === 1);
    fakes[0]?.exit();
    expect(await running).toEqual(HOST_STOPPED);
  });

  it("answers unavailable when a host exits before it was ready", async () => {
    const executor = toolHosts({
      spawnHost: fakeSpawn((host, message) => {
        if (message.type === "start") host.exit();
      }),
    });
    expect(await executor.run(resolve(), signal())).toMatchObject(unavailable);
  });

  it("runs a host on the app's own node, with none of the app's environment but its PATH and HOME", async () => {
    // What the host's node was given: as a script of the host's, it says so and goes.
    const script = join(base, "says.cjs");
    writeFileSync(script, "process.send({ type: 'said', node: process.execPath, title: process.title, env: Object.keys(process.env).sort() }, () => process.exit(0));\n");
    process.env.NODE_OPTIONS = "--title=leaked";
    process.env.OPENSSL_CONF = join(base, "openssl.cnf");
    try {
      const host = forkHost({ script });
      const said = await new Promise((resolve) => host.onMessage(resolve));
      expect(said).toEqual({
        type: "said", node: NODE, title: expect.not.stringContaining("leaked"),
        env: ["HOME", "PATH"],
      });
    } finally {
      delete process.env.NODE_OPTIONS;
      delete process.env.OPENSSL_CONF;
    }
  });

  it("runs a host whose node opens no inspector on SIGUSR1, as Electron's fuse keeps its own from opening one", async () => {
    // A script of the host's: it says its pid, then, asked, whether an inspector listens. On port 0,
    // so that no other process's 9229 keeps one from opening.
    const script = join(base, "inspected.cjs");
    writeFileSync(script, [
      "process.debugPort = 0;",
      "process.on('message', () => process.send({ type: 'inspector', url: require('node:inspector').url() ?? null }));",
      "process.send({ type: 'pid', pid: process.pid });",
    ].join("\n"));
    const host = forkHost({ script });
    const said: Array<{ type: string; pid?: number; url?: string | null }> = [];
    host.onMessage((message) => said.push(message as unknown as (typeof said)[number]));
    try {
      await until(() => said.length > 0, 5_000);
      process.kill(said[0]!.pid!, "SIGUSR1");
      await new Promise((resolve) => setTimeout(resolve, 500));
      host.send({ type: "stop" });
      await until(() => said.length > 1, 5_000);
      expect(said[1]).toEqual({ type: "inspector", url: null });
    } finally {
      host.kill();
    }
  });

  it("answers unavailable when the host cannot be spawned at all, and stops", async () => {
    const executor = toolHosts({ spawnHost: () => forkHost({ execPath: "/nonexistent/node" }) });
    expect(await executor.run(resolve(), signal())).toMatchObject(unavailable);
    await executor.stop();
  });

  it("answers unavailable when the host's script dies at once", async () => {
    const script = join(base, "dies.js");
    writeFileSync(script, "process.exit(3);\n");
    const executor = toolHosts({ spawnHost: () => forkHost({ script }) });
    expect(await executor.run(resolve(), signal())).toMatchObject(unavailable);
    await executor.stop();
  });

  it("tells a host's listeners once that it never started", async () => {
    const host = forkHost({ execPath: "/nonexistent/node" });
    let calls = 0;
    host.onExit(() => {
      calls += 1;
    });
    await until(() => calls > 0, 2_000);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(calls).toBe(1);
    host.kill();
  });

  it("says once why a host could not be spawned", async () => {
    const written = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
    try {
      const host = forkHost({ execPath: "/nonexistent/node" });
      let gone = false;
      host.onExit(() => {
        gone = true;
      });
      await until(() => gone, 2_000);
      await new Promise((resolve) => setTimeout(resolve, 100));
      const said = written.mock.calls.map(([text]) => String(text)).filter((text) => text.startsWith("the file host"));
      expect(said).toEqual(["the file host could not start: spawn /nonexistent/node ENOENT\n"]);
      host.kill();
    } finally {
      written.mockRestore();
    }
  });

  it("tells a host's listeners once that it has gone, and signals no group after that", async () => {
    const script = join(base, "dies.js");
    writeFileSync(script, "process.exit(3);\n");
    const kill = vi.spyOn(process, "kill");
    try {
      const host = forkHost({ script });
      let calls = 0;
      host.onExit(() => {
        calls += 1;
      });
      await until(() => calls > 0, 2_000);
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(calls).toBe(1);
      const before = kill.mock.calls.length;
      host.kill();
      expect(kill.mock.calls.length).toBe(before);
    } finally {
      kill.mockRestore();
    }
  });

  it("answers every operation that is running when it stops, and stop resolves", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(readyOnly) });
    const first = executor.run(resolve(), signal());
    const second = executor.run(resolve(), signal());
    await until(() => sent(0, "op") === 2);
    await executor.stop();
    expect(await first).toEqual(HOST_STOPPED);
    expect(await second).toEqual(HOST_STOPPED);
  });

  it("ends every host without stopping, and starts a new one for the next operation", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering) });
    await executor.run(resolve(), signal());
    await executor.run(op("resolve", { path: "" }, ROOT_B), signal());
    await executor.end();
    expect([sent(0, "stop"), sent(1, "stop")]).toEqual([1, 1]);
    expect(await executor.run(resolve(), signal())).toEqual({ ok: expect.any(String) });
    expect(fakes).toHaveLength(3);
  });

  it("makes a second stop wait for the first", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn((host, message) => {
      if (message.type === "start") host.say({ type: "ready", processes: [] });
    }) });
    const running = executor.run(resolve(), signal());
    await until(() => sent(0, "op") === 1);
    const first = executor.stop();
    let secondDone = false;
    const second = executor.stop().then(() => {
      secondDone = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(secondDone).toBe(false);
    fakes[0]?.exit();
    await Promise.all([first, second]);
    expect(await running).toEqual(HOST_STOPPED);
    expect(sent(0, "stop")).toBe(1);
  });

  it("answers a cancel at once while the host is still starting, and uses the host once it is ready", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn((host, message) => {
      onStop(host, message);
      if (message.type === "op") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
    }) });
    const controller = new AbortController();
    const running = executor.run(resolve(), controller.signal);
    await until(() => fakes.length === 1);
    controller.abort();
    expect(await running).toEqual(CANCELLED);
    const aborted = new AbortController();
    aborted.abort();
    expect(await executor.run(resolve(), aborted.signal)).toEqual(CANCELLED);
    expect(fakes[0]?.count("op")).toBe(0);
    fakes[0]?.say({ type: "ready", processes: [] });
    const next = resolve();
    expect(await executor.run(next, signal())).toEqual({ ok: next.id });
    expect(fakes).toHaveLength(1);
  });

  it("runs concurrent operations for one root on one host", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering) });
    const [a, b] = [resolve(), resolve()];
    expect(await Promise.all([executor.run(a, signal()), executor.run(b, signal())])).toEqual([{ ok: a.id }, { ok: b.id }]);
    expect(fakes).toHaveLength(1);
    expect(sent(0, "op")).toBe(2);
  });

  it("tells a host the identity its folder was bound with", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering) });
    await executor.run(resolve(), signal());
    expect(fakes[0]?.sent[0]).toMatchObject({
      type: "start", folder: folders[ROOT_A], expect: identities.get(folders[ROOT_A] ?? ""),
    });
  });

  it("puts its root's network asks to the approvals for the root while something of it runs, failing closed", async () => {
    const asked: Array<[string, NetworkAsk]> = [];
    // The last throws at once, without a promise.
    const choices: Array<NetworkAnswer | Error | "throw"> = ["allow", "allow_session", "deny", new Error("no display"), "throw"];
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      network: {
        askNetwork: (root, request) => {
          asked.push([root, request]);
          const choice = choices.shift();
          if (choice === "throw") throw new Error("boom");
          return choice instanceof Error ? Promise.reject(choice) : Promise.resolve(choice ?? "deny");
        },
      },
    });
    await executor.run(resolve(), signal());
    // A process of the root's lives in the guest: its connections may ask.
    executor.processes(ROOT_A, { handles: [], live: 1 });
    const answers: NetworkAnswer[] = [];
    for (const privateNetwork of [false, false, false, true, false]) {
      answers.push(await executor.ask(ROOT_A, { host: "example.com", port: 443, privateNetwork }));
    }
    expect(answers).toEqual(["allow", "allow_session", "deny", "deny", "deny"]);
    expect(asked.map(([root, request]) => [root, request.privateNetwork])).toEqual([
      [ROOT_A, false], [ROOT_A, false], [ROOT_A, false], [ROOT_A, true], [ROOT_A, false],
    ]);
    expect(asked[0]?.[1]).toEqual({ host: "example.com", port: 443, privateNetwork: false });
    // A root with no host here has nothing that runs: denied, unasked.
    expect(await executor.ask(ROOT_B, { host: "example.com", port: 443, privateNetwork: false })).toBe("deny");
    expect(asked).toHaveLength(5);
  });

  it("dismisses the prompt of an ask that comes in the same moment as its root's last process ends", async () => {
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      // A prompt that stays open until it is dismissed.
      network: {
        askNetwork: (_root, _request, dismissed) => new Promise((done) => {
          if (dismissed.aborted) done("deny");
          dismissed.addEventListener("abort", () => done("deny"));
        }),
      },
    });
    await executor.run(resolve(), signal());
    executor.processes(ROOT_A, { handles: [], live: 1 });
    // The ask, then its root's last process ended, before its prompt was shown.
    const answer = executor.ask(ROOT_A, { host: "example.com", port: 443, privateNetwork: false });
    executor.processes(ROOT_A, { handles: [], live: 0 });
    expect(await answer).toBe("deny");
  });

  it("dismisses a host's open network prompt when the host goes", async () => {
    let prompt: AbortSignal | undefined;
    const executor = toolHosts({
      spawnHost: fakeSpawn(readyOnly),
      network: {
        askNetwork: (_root, _request, dismissed) => {
          prompt = dismissed;
          return new Promise((settle) => dismissed.addEventListener("abort", () => settle("deny"), { once: true }));
        },
      },
    });
    // The operation that asks is still running when its host goes.
    const running = executor.run(resolve(), signal());
    await until(() => sent(0, "op") === 1);
    const answer = executor.ask(ROOT_A, { host: "example.com", port: 443, privateNetwork: false });
    await until(() => prompt !== undefined);
    expect(prompt?.aborted).toBe(false);
    // The computer's access ended: every host stops.
    await executor.end();
    expect([prompt?.aborted, await answer, await running]).toEqual([true, "deny", HOST_STOPPED]);
  });

  it("gives the binder the folder guards its hosts start with, and none without a HOME", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering), appDirs: [join(base, "app")] });
    await executor.run(resolve(), signal());
    const start = fakes[0]?.sent[0];
    expect(start?.type).toBe("start");
    if (start?.type !== "start") return;
    expect(executor.guards()).toEqual({ home: start.env.HOME, dataDir: start.dataDir, appDirs: start.appDirs });
    expect(toolHosts({ appDirs: undefined }).guards().appDirs).toEqual(APP_DIRS);
    expect(() => toolHosts({ env: { PATH: "/usr/bin:/bin" } }).guards()).toThrow("the app's environment has no HOME");
  });

  it("looks up no folder for an ill-formed session id", async () => {
    let looked = 0;
    const executor = toolHosts({
      bindingOf: () => {
        looked += 1;
        return { folder: folders[ROOT_A] ?? "", dev: 0, ino: 0, boot: "" };
      },
      spawnHost: fakeSpawn(answering),
    });
    for (const id of ["../../etc", "", `${ROOT_A}/..`, ROOT_A.toUpperCase().replace(/-/g, "_")]) {
      expect(await executor.run(op("stat", { key: "/x" }, id), signal())).toEqual(FOLDER_UNAVAILABLE);
    }
    expect(looked).toBe(0);
    expect(fakes).toHaveLength(0);
  });

  it("does not let an abort after an answer touch a newer operation with the same id", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(readyOnly) });
    const again = { ...resolve(), id: "again" };
    const stale = new AbortController();
    const first = executor.run(again, stale.signal);
    await until(() => sent(0, "op") === 1);
    fakes[0]?.say({ type: "result", id: "again", outcome: { ok: 1 } });
    expect(await first).toEqual({ ok: 1 });
    const second = executor.run(again, signal());
    await until(() => sent(0, "op") === 2);
    stale.abort();
    expect(sent(0, "cancel")).toBe(0);
    fakes[0]?.say({ type: "result", id: "again", outcome: { ok: 2 } });
    expect(await second).toEqual({ ok: 2 });
  });

  it("stops a host that has had nothing to do for a while, and starts another for the next operation", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn(answering) });
    expect(await executor.run(resolve(), signal())).toMatchObject({ ok: expect.any(String) });
    await until(() => sent(0, "stop") === 1, 1_000);
    expect(await executor.run(resolve(), signal())).toMatchObject({ ok: expect.any(String) });
    expect(fakes.length).toBe(2);
  });

  it("does not stop a host while an operation runs", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn(readyOnly) });
    void executor.run(resolve(), signal());
    await new Promise((done) => setTimeout(done, 200));
    expect(sent(0, "stop")).toBe(0);
  });

  it("does not stop a host while a later operation runs", async () => {
    const executor = toolHosts({ idleMs: 100, spawnHost: fakeSpawn((host, message) => {
      readyOnly(host, message);
      if (message.type === "op" && message.kind === "resolve") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
    }) });
    await executor.run(resolve(), signal());
    void executor.run(op("stat", { key: "/x" }), signal());
    await new Promise((done) => setTimeout(done, 250));
    expect(sent(0, "stop")).toBe(0);
  });

  it("names the roots whose processes live in the VM, and says each time they change, or a host goes", async () => {
    let changes = 0;
    const executor = toolHosts({ spawnHost: fakeSpawn(answering), changed: () => void (changes += 1) });
    expect(await executor.run(resolve(), signal())).toMatchObject({ ok: expect.any(String) });
    expect(executor.liveRoots()).toEqual([]);
    const handle = { id: "proc_000000000001", command: "sleep 9", cwd: "/", task_id: null, started_at: Date.now() / 1000 };
    executor.processes(ROOT_A, { handles: [handle], live: 1 });
    expect([executor.liveRoots(), changes]).toEqual([[ROOT_A], 1]);
    executor.processes(ROOT_A, { gone: true });
    expect([executor.liveRoots(), changes]).toEqual([[], 2]);
    executor.processes(ROOT_A, { handles: [handle], live: 1 });
    fakes[0]?.exit();
    await until(() => changes === 4);
    expect(executor.liveRoots()).toEqual([]);
  });

  it("does not stop a host while its root's processes in the guest run, the count coming after its last answer, and stops it once they end", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn(answering) });
    await executor.run(resolve(), signal());
    // The idle stop is already armed when the count comes.
    executor.processes(ROOT_A, { handles: [], live: 1 });
    await new Promise((done) => setTimeout(done, 250));
    expect(sent(0, "stop")).toBe(0);
    executor.processes(ROOT_A, { handles: [], live: 0 });
    await until(() => sent(0, "stop") === 1, 1_000);
  });

  it("stops a host that has had nothing to do once it is ready, whatever it heard of its root's processes elsewhere before", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn((host, message) => {
      onStop(host, message);
      if (message.type === "op") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
    }) });
    const answer = executor.run(resolve(), signal());
    // A process alive in a guest that the root's earlier host used: the ready's handles are the record's, all ended.
    executor.processes(ROOT_A, { handles: [{ id: "proc_000000000001", command: "sleep 9", cwd: "/", task_id: null, started_at: Date.now() / 1000 }], live: 1 });
    fakes[0]?.say({ type: "ready", processes: [] });
    expect(await answer).toMatchObject({ ok: expect.any(String) });
    await until(() => sent(0, "stop") === 1, 1_000);
    expect(sent(0, "handles")).toBe(0);
  });

  it("waits, when the app quits, for a host that is stopping because it had nothing to do", async () => {
    const slowStop = (host: FakeHost, message: ToHost) => {
      if (message.type === "start") host.say({ type: "ready", processes: [] });
      if (message.type === "op") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
      if (message.type === "stop") setTimeout(() => host.exit(), 300);
    };
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn(slowStop) });
    await executor.run(resolve(), signal());
    await until(() => sent(0, "stop") === 1, 1_000);
    // The first host is still exiting: the next operation goes to a new one.
    expect(await executor.run(resolve(), signal())).toMatchObject({ ok: expect.any(String) });
    expect(fakes.length).toBe(2);
    let exited = false;
    fakes[0]?.onExit(() => {
      exited = true;
    });
    await executor.stop();
    expect(exited).toBe(true);
  });
});
