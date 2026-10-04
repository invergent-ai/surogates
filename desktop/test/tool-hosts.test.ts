import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Operation } from "../src/link/protocol.js";
import { FOLDER_UNAVAILABLE, type FromHost, type ToHost } from "../src/hosts/messages.js";
import {
  CANCELLED, forkHost, HOST_STOPPED, type HostProcess, NOT_BOUND, START_TIMEOUT_MS, ToolHosts, type ToolHostsOptions,
} from "../src/hosts/tool-hosts.js";

const ROOT_A = "11111111-1111-4111-8111-111111111111";
const ROOT_B = "22222222-2222-4222-8222-222222222222";
const SLEEP = "37.25";

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
let spawned: HostProcess[];
let exits: number;
let hosts: ToolHosts | null;

const op = (kind: string, args: Record<string, unknown>, sessionId = ROOT_A): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId, callingSessionId: sessionId, invocationId: "call", ordinal: 1, kind, args, digest: "d",
});

function toolHosts(overrides: Partial<ToolHostsOptions> = {}): ToolHosts {
  hosts = new ToolHosts({
    bindingOf: (root) => {
      const folder = folders[root];
      return folder ? { folder } : undefined;
    },
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: `${folders[ROOT_A]}/bin:/usr/bin:/bin` },
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
  for (const folder of Object.values(folders)) mkdirSync(folder);
  // An rg that never answers, for operations that are still running; its sleep
  // has a length only it uses, so the test can see it run and stop.
  mkdirSync(join(folders[ROOT_A] ?? "", "bin"));
  writeFileSync(join(folders[ROOT_A] ?? "", "bin", "rg"), `#!/bin/sh\nexec sleep ${SLEEP}\n`, { mode: 0o755 });
  spawned = [];
  exits = 0;
  hosts = null;
});

afterEach(async () => {
  await hosts?.stop();
  // A host that failed to start, or timed out, is no longer in hosts and is on its way out. srt
  // cleans up /tmp only when a host exits by itself, so each is given the time before any is killed.
  await until(() => exits >= spawned.length, 3_000).catch(() => {});
  for (const host of spawned) host.kill();
  rmSync(base, { recursive: true, force: true });
});

const slowSearch = () => op("ripgrep", { key: folders[ROOT_A], mode: "files", pattern: "*", glob: null, context: 0 });

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
    const first = executor.run(op("run", { command: "sleep 31.5", workdir: null, timeout: 60 }), busy.signal);
    await until(() => Number(spawnSync("pgrep", ["-fc", "^sleep 31.5$"], { encoding: "utf8" }).stdout.trim() || 0) >= 1, 20_000);
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
  if (message.type === "start") host.say({ type: "ready" });
  onStop(host, message);
};
const answering = (host: FakeHost, message: ToHost) => {
  readyOnly(host, message);
  if (message.type === "op") host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
};

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

  it("makes a second stop wait for the first", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn((host, message) => {
      if (message.type === "start") host.say({ type: "ready" });
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
    fakes[0]?.say({ type: "ready" });
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

  it("looks up no folder for an ill-formed session id", async () => {
    let looked = 0;
    const executor = toolHosts({
      bindingOf: () => {
        looked += 1;
        return { folder: folders[ROOT_A] ?? "" };
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

  it("waits, when the app quits, for a host that is stopping because it had nothing to do", async () => {
    const slowStop = (host: FakeHost, message: ToHost) => {
      if (message.type === "start") host.say({ type: "ready" });
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
