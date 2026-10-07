import { spawnSync } from "node:child_process";
import { lstatSync, mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { type BindMode, MAX_PROTECTED, type ProtectedKey } from "../src/guest/protocol.js";
import type { Operation } from "../src/link/protocol.js";
import { FOLDER_UNAVAILABLE, type FromHost, type NetworkAnswer, type NetworkAsk, type ToHost } from "../src/hosts/messages.js";
import { guestBinds } from "../src/hosts/restarts.js";
import {
  APP_DIRS, CANCELLED, forkHost, HOST_STOPPED, type HostProcess, NOT_BOUND, START_TIMEOUT_MS, ToolHosts,
  type ToolHostsOptions,
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
// Each folder's identity as it was made: the one its binding holds.
let identities: Map<string, { dev: number; ino: number; boot: string }>;
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
      const made = folder === undefined ? undefined : identities.get(folder);
      return folder && made ? { folder, ...made } : undefined;
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
  identities = new Map();
  for (const folder of Object.values(folders)) {
    mkdirSync(folder);
    const { dev, ino } = statSync(folder);
    identities.set(folder, { dev, ino, boot: BOOT_ID });
  }
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

describe("a folder's protected keys, for a guest's read-only binds", { timeout: 30_000 }, () => {
  const RAN = { ok: { output: "", returncode: 0, timed_out: false } };
  const key = (path: string, mode: BindMode = "ro"): ProtectedKey => [join(folders[ROOT_A]!, path), lstatSync(join(folders[ROOT_A]!, path)).ino, mode];
  // A command elsewhere (the VM), and the keys it was given.
  const command = (executor: ToolHosts, carried: ProtectedKey[][]) => executor.guarded(op("run", { command: "true" }), signal(), "around", async (_b, _s, _e, keys) => {
    carried.push(keys);
    return RAN;
  });

  beforeEach(() => {
    const a = folders[ROOT_A]!;
    mkdirSync(join(a, ".git", "hooks"), { recursive: true });
    writeFileSync(join(a, ".git", "config"), "[core]\n");
    writeFileSync(join(a, ".git", "hooks", "pre-commit.sample"), "");
    mkdirSync(join(a, "sub", ".vscode"), { recursive: true });
    writeFileSync(join(a, "sub", ".vscode", "settings.json"), "{}\n");
  });

  it("come with each command, the outermost of each kind with its inode, named again when a host program has replaced one", async () => {
    const told: Array<[string, ProtectedKey[]]> = [];
    const executor = toolHosts({ protect: (root, keys) => void told.push([root, keys]) });
    const carried: ProtectedKey[][] = [];
    expect(await command(executor, carried)).toEqual(RAN);
    const first = [key(".git", "rw"), key(".git/config"), key(".git/hooks"), key("sub/.vscode")];
    expect(carried).toEqual([first]);
    expect(told.at(-1)).toEqual([ROOT_A, first]);
    // git config on the host renames a new file over the old one; no look has run since.
    writeFileSync(join(folders[ROOT_A]!, ".git", "config.lock"), "[core]\n\teditor = true\n");
    renameSync(join(folders[ROOT_A]!, ".git", "config.lock"), join(folders[ROOT_A]!, ".git", "config"));
    await command(executor, carried);
    expect(carried[1]).toEqual([key(".git", "rw"), key(".git/config"), key(".git/hooks"), key("sub/.vscode")]);
    expect(carried[1]?.[1]?.[1]).not.toBe(first[1]?.[1]);
  });

  it("are told as a look between commands finds a new one, as something running elsewhere may make it", async () => {
    const told: ProtectedKey[][] = [];
    const executor = toolHosts({ protect: (_root, keys) => void told.push(keys) });
    await command(executor, []);
    writeFileSync(join(folders[ROOT_A]!, ".mcp.json"), "{}\n");
    // The look every 5 s while a command of the root's has run elsewhere.
    await until(() => told.at(-1)?.some(([path]) => path.endsWith("/.mcp.json")) === true, 15_000);
    expect(told.at(-1)).toEqual([key(".git", "rw"), key(".git/config"), key(".git/hooks"), key(".mcp.json"), key("sub/.vscode")]);
  });

  it("hold each folder above a key that holds protected names or lies in a .git, a nested repository's too, and no ordinary folder", () => {
    const f = "/home/ana/project";
    const keys = [".git/config", ".git/hooks/pre-commit", ".git/modules/lib/config", ".claude/commands/x.md", "sub/.git/config", "sub/.vscode/settings.json"];
    expect(guestBinds(f, keys.map((key) => join(f, key)))).toEqual([
      [`${f}/.claude`, "rw"], [`${f}/.claude/commands`, "ro"],
      [`${f}/.git`, "rw"], [`${f}/.git/config`, "ro"], [`${f}/.git/hooks`, "ro"],
      [`${f}/.git/modules`, "rw"], [`${f}/.git/modules/lib`, "rw"], [`${f}/.git/modules/lib/config`, "ro"],
      [`${f}/sub/.git`, "rw"], [`${f}/sub/.git/config`, "ro"], [`${f}/sub/.vscode`, "ro"],
    ]);
  });

  it("leave git's own working state unbound, a submodule's too, but for what sends a linked worktree to a config", () => {
    const f = "/home/ana/project";
    const keys = [
      ".git/config", ".git/rebase-merge/git-rebase-todo", ".git/REBASE-APPLY/patch", ".git/sequencer/todo",
      ".git/worktrees/wt/commondir", ".git/worktrees/wt/config.worktree", ".git/worktrees/wt/gitdir", ".git/worktrees/wt/HEAD",
      ".git/worktrees/wt/rebase-merge/done", ".git/worktrees/wt/logs/HEAD",
      ".git/modules/lib/config", ".git/modules/lib/rebase-merge/done", ".git/modules/lib/worktrees/w2/COMMONDIR", "sub/.git/sequencer/head",
    ];
    expect(guestBinds(f, keys.map((key) => join(f, key)))).toEqual([
      [`${f}/.git`, "rw"], [`${f}/.git/config`, "ro"],
      [`${f}/.git/modules`, "rw"], [`${f}/.git/modules/lib`, "rw"], [`${f}/.git/modules/lib/config`, "ro"],
      [`${f}/.git/modules/lib/worktrees`, "rw"], [`${f}/.git/modules/lib/worktrees/w2`, "rw"], [`${f}/.git/modules/lib/worktrees/w2/COMMONDIR`, "ro"],
      [`${f}/.git/worktrees`, "rw"], [`${f}/.git/worktrees/wt`, "rw"],
      [`${f}/.git/worktrees/wt/commondir`, "ro"], [`${f}/.git/worktrees/wt/config.worktree`, "ro"],
    ]);
  });

  it("name what a link at a protected name leads to in the folder, nothing for one that leads out of it, and the link itself for one that leads to nothing there", async () => {
    const a = folders[ROOT_A] ?? "";
    // An editor's settings shared with a sibling worktree, out of the folder.
    mkdirSync(join(base, "shared-vscode"));
    rmSync(join(a, "sub", ".vscode"), { recursive: true });
    symlinkSync(join(base, "shared-vscode"), join(a, "sub", ".vscode"));
    writeFileSync(join(a, "mcp.json"), "{}\n");
    symlinkSync("mcp.json", join(a, ".mcp.json"));
    symlinkSync("missing", join(a, ".idea"));
    symlinkSync(join(base, "nowhere"), join(a, ".zshrc"));
    const carried: ProtectedKey[][] = [];
    expect(await command(toolHosts({ protect: () => {} }), carried)).toEqual(RAN);
    expect(carried).toEqual([[key(".git", "rw"), key(".git/config"), key(".git/hooks"), key(".idea"), key("mcp.json")]]);
  });

  it("are not named for an executor that binds none", async () => {
    const carried: ProtectedKey[][] = [];
    await command(toolHosts(), carried);
    expect(carried).toEqual([[]]);
  });

  it("refuse commands past the most the sandbox makes read-only", async () => {
    // Each repository's config, and its .git held above it.
    for (let n = 0; n < MAX_PROTECTED / 2; n += 1) {
      mkdirSync(join(folders[ROOT_A]!, `r${n}`, ".git"), { recursive: true });
      writeFileSync(join(folders[ROOT_A]!, `r${n}`, ".git", "config"), "");
    }
    const ran: ProtectedKey[][] = [];
    expect(await command(toolHosts({ protect: () => {} }), ran)).toEqual({
      error: {
        type: "sandbox",
        message: `Blocked: the computer could not check this folder's protected paths, so commands cannot run here: this folder has ${MAX_PROTECTED + 4} protected paths, and the sandbox can make at most ${MAX_PROTECTED} read-only`,
      },
    });
    expect(ran).toEqual([]);
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
  if (message.type === "start") host.say({ type: "ready", processes: [] });
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

  it("puts a host's network asks to the approvals for its root, and tells the host each answer, failing closed", async () => {
    const asked: Array<[string, NetworkAsk]> = [];
    // The last throws at once, without a promise.
    const choices: Array<NetworkAnswer | Error | "throw"> = [
      "allow", "allow_session", "deny", new Error("no display"), "maybe" as NetworkAnswer, "throw",
    ];
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      network: {
        granted: () => [],
        askNetwork: (root, request) => {
          asked.push([root, request]);
          const choice = choices.shift();
          if (choice === "throw") throw new Error("boom");
          return choice instanceof Error ? Promise.reject(choice) : Promise.resolve(choice ?? "deny");
        },
      },
    });
    await executor.run(resolve(), signal());
    // A background process lives: its connections may ask.
    fakes[0]?.say({ type: "processes", live: 1 });
    for (let id = 1; id <= 6; id += 1) fakes[0]?.say({ type: "ask", id, host: "example.com", port: 443, privateNetwork: id === 5 });
    await until(() => sent(0, "answer") === 6);
    const answers = fakes[0]?.sent.filter((message) => message.type === "answer") ?? [];
    expect(answers.sort((a, b) => a.id - b.id)).toEqual([
      { type: "answer", id: 1, allow: true, remember: false },
      { type: "answer", id: 2, allow: true, remember: true },
      { type: "answer", id: 3, allow: false, remember: false },
      { type: "answer", id: 4, allow: false, remember: false },
      { type: "answer", id: 5, allow: false, remember: false },
      { type: "answer", id: 6, allow: false, remember: false },
    ]);
    expect(asked.map(([root, request]) => [root, request.privateNetwork])).toEqual([
      [ROOT_A, false], [ROOT_A, false], [ROOT_A, false], [ROOT_A, false], [ROOT_A, true], [ROOT_A, false],
    ]);
    expect(asked[0]?.[1]).toEqual({ host: "example.com", port: 443, privateNetwork: false });
  });

  it("refuses every destination off the package hosts when it has no approvals to ask", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering) });
    await executor.run(resolve(), signal());
    fakes[0]?.say({ type: "processes", live: 1 });
    fakes[0]?.say({ type: "ask", id: 1, host: "example.com", port: 443, privateNetwork: false });
    await until(() => sent(0, "answer") === 1);
    expect(fakes[0]?.sent.at(-1)).toEqual({ type: "answer", id: 1, allow: false, remember: false });
    expect(fakes[0]?.sent[0]).toMatchObject({ type: "start", domains: [] });
  });

  it("denies an ask that comes once nothing of its root runs, without a prompt", async () => {
    const asked: NetworkAsk[] = [];
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      network: {
        granted: () => [],
        askNetwork: (_root, request) => {
          asked.push(request);
          return Promise.resolve("allow");
        },
      },
    });
    await executor.run(resolve(), signal());
    // Late: its lookup outlasted the command that asked, which has answered.
    fakes[0]?.say({ type: "ask", id: 1, host: "example.com", port: 443, privateNetwork: false });
    await until(() => sent(0, "answer") === 1);
    expect(fakes[0]?.sent.at(-1)).toEqual({ type: "answer", id: 1, allow: false, remember: false });
    expect(asked).toEqual([]);
  });

  it("dismisses the prompt of an ask that comes in the same moment as its root's last process ends", async () => {
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      // A prompt that stays open until it is dismissed.
      network: {
        granted: () => [],
        askNetwork: (_root, _request, dismissed) => new Promise((done) => {
          if (dismissed.aborted) done("deny");
          dismissed.addEventListener("abort", () => done("deny"));
        }),
      },
    });
    await executor.run(resolve(), signal());
    fakes[0]?.say({ type: "processes", live: 1 });
    // As child-process IPC hands over the messages of one read: the ask, then its root's last process ended.
    fakes[0]?.say({ type: "ask", id: 1, host: "example.com", port: 443, privateNetwork: false });
    fakes[0]?.say({ type: "processes", live: 0 });
    await until(() => sent(0, "answer") === 1, 1_000);
    expect(fakes[0]?.sent.at(-1)).toEqual({ type: "answer", id: 1, allow: false, remember: false });
  });

  it("starts each host with what its root's user allowed for the chat", async () => {
    const executor = toolHosts({
      spawnHost: fakeSpawn(answering),
      network: { granted: (root) => (root === ROOT_A ? ["example.com"] : []), askNetwork: () => Promise.resolve("deny") },
    });
    await executor.run(resolve(), signal());
    await executor.run(op("resolve", { path: "" }, ROOT_B), signal());
    expect(fakes.map((fake) => fake.sent[0])).toMatchObject([
      { type: "start", domains: ["example.com"] }, { type: "start", domains: [] },
    ]);
  });

  it("dismisses a host's open network prompt when the host goes", async () => {
    let prompt: AbortSignal | undefined;
    const executor = toolHosts({
      spawnHost: fakeSpawn(readyOnly),
      network: {
        granted: () => [],
        askNetwork: (_root, _request, dismissed) => {
          prompt = dismissed;
          return new Promise((settle) => dismissed.addEventListener("abort", () => settle("deny"), { once: true }));
        },
      },
    });
    // The command that asks is still running when its host goes.
    const running = executor.run(resolve(), signal());
    await until(() => sent(0, "op") === 1);
    fakes[0]?.say({ type: "ask", id: 1, host: "example.com", port: 443, privateNetwork: false });
    await until(() => prompt !== undefined);
    expect(prompt?.aborted).toBe(false);
    // The computer's access ended: every host stops.
    await executor.end();
    expect(prompt?.aborted).toBe(true);
    expect(await running).toEqual(HOST_STOPPED);
  });

  it("dismisses a host's open network prompts once nothing of its root runs", async () => {
    let prompt: AbortSignal | undefined;
    const executor = toolHosts({
      spawnHost: fakeSpawn(readyOnly),
      network: {
        granted: () => [],
        askNetwork: (_root, _request, dismissed) => {
          prompt = dismissed;
          return new Promise((settle) => dismissed.addEventListener("abort", () => settle("deny"), { once: true }));
        },
      },
    });
    const command = new AbortController();
    const running = executor.run(resolve(), command.signal);
    await until(() => sent(0, "op") === 1);
    fakes[0]?.say({ type: "ask", id: 1, host: "example.com", port: 443, privateNetwork: false });
    await until(() => prompt !== undefined);
    expect(prompt?.aborted).toBe(false);
    // The command that asked ends: no connection of the root can still wait.
    command.abort();
    await running;
    expect(prompt?.aborted).toBe(true);
    // The dismissed prompt's denial is the host's answer.
    await until(() => fakes[0]?.sent.at(-1)?.type === "answer");
    expect(fakes[0]?.sent.at(-1)).toEqual({ type: "answer", id: 1, allow: false, remember: false });
  });

  it("gives the binder the folder guards its hosts start with, and none without a HOME", async () => {
    const executor = toolHosts({ spawnHost: fakeSpawn(answering), appDirs: [join(base, "app")] });
    await executor.run(resolve(), signal());
    const start = fakes[0]?.sent[0];
    expect(start?.type).toBe("start");
    if (start?.type !== "start") return;
    expect(executor.guards()).toEqual({ home: start.env.HOME, dataDir: start.dataDir, appDirs: start.appDirs });
    expect(toolHosts().guards().appDirs).toEqual(APP_DIRS);
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

  it("does not stop a host while its background processes run, and stops it once they end", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn((host, message) => {
      readyOnly(host, message);
      if (message.type === "op") {
        host.say({ type: "processes", live: 1 });
        host.say({ type: "result", id: message.id, outcome: { ok: message.id } });
      }
    }) });
    await executor.run(op("start", { command: "sleep 1" }), signal());
    await new Promise((done) => setTimeout(done, 250));
    expect(sent(0, "stop")).toBe(0);
    fakes[0]?.say({ type: "processes", live: 0 });
    await until(() => sent(0, "stop") === 1, 1_000);
  });

  it("does not stop a host whose processes count comes after its last operation's answer", async () => {
    const executor = toolHosts({ idleMs: 50, spawnHost: fakeSpawn(answering) });
    await executor.run(op("start", { command: "sleep 1" }), signal());
    // The idle stop is already armed when the count comes.
    fakes[0]?.say({ type: "processes", live: 1 });
    await new Promise((done) => setTimeout(done, 250));
    expect(sent(0, "stop")).toBe(0);
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
