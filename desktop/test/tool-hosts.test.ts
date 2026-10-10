import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { downloadSaver } from "../src/browser/downloads.js";
import { kinds, perform } from "../src/files/operations.js";
import { type Copy, type Handle, historyOff, type Opened } from "../src/history/copies.js";
import { keyOf } from "../src/history/place.js";
import type { Binding } from "../src/journal/bindings.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { FOLDER_UNAVAILABLE, type FromHost, type NetworkAnswer, type NetworkAsk, type ToHost } from "../src/hosts/messages.js";
import {
  APP_DIRS, CANCELLED, forkHost, type Guard, HOST_STOPPED, type HostProcess, NODE, NOT_BOUND, START_TIMEOUT_MS, ToolHosts,
  type ToolHostsOptions,
} from "../src/hosts/tool-hosts.js";
import { asItLies } from "./as-it-lies.js";

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
    cacheDir: join(base, "cache", "surogate"),
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
    // In Section 9's words, never srt's.
    const missing: Outcome = {
      error: { type: "unavailable", message: "This computer could not open the folder's sandbox: Surogate's sandbox tools are missing. Run the install script again. It lacks bubblewrap" },
    };
    expect(await executor.run(op("stat", { key: "/x" }), signal())).toEqual(missing);
    expect(await executor.run(op("stat", { key: "/x" }), signal())).toEqual(missing);
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
// Answers as well what the hook guard is asked around a command: nothing refused, and the command's outcome after it.
const guarding = (host: FakeHost, message: ToHost) => {
  answering(host, message);
  if (message.type === "refusal") host.say({ type: "result", id: message.id, outcome: { ok: null } });
  if (message.type === "after") host.say({ type: "result", id: message.id, outcome: message.outcome });
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
    const written = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const host = forkHost({ execPath: "/nonexistent/node" });
      let gone = false;
      host.onExit(() => {
        gone = true;
      });
      await until(() => gone, 2_000);
      await new Promise((resolve) => setTimeout(resolve, 100));
      const said = written.mock.calls.map(([text]) => String(text)).filter((text) => text.startsWith("the file host"));
      expect(said).toEqual(["the file host could not start: /nonexistent/node is not there, or cannot be run"]);
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
    expect(executor.guards()).toEqual({ home: start.env.HOME, dataDir: start.dataDir, cacheDir: start.cacheDir, appDirs: start.appDirs });
    expect(start.cacheDir).toBe(join(base, "cache", "surogate"));
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


// A project's thread works in its copy of its folder (spec, Section 13): its host is started on the copy, once the
// copies have it made, and is asked by the folder's path. Fakes stand for the hosts and for the copies: what a real
// host does with its start is host-start.test.ts's, and the real hosts' own run is below.
describe("a root bound to a thread's copy of a folder", { timeout: 5_000 }, () => {
  const KEY = "0123456789abcdef";
  // Every kind a root's operation can have that runs no command: the file helper's, a thread's own, and one that is none.
  const KINDS = [...kinds(), "history", "checkpoint", "no-such-kind"];
  let fakes: FakeHost[];
  // How many of them have exited.
  let gone: number;
  let opens: string[];
  // Each hold on a copy as it was let go, with how many hosts had exited and which roots the guest had let go by then.
  let closed: Array<{ handle: Handle; gone: number; released: string[] }>;
  let released: string[];
  // How often the guest made each root's copy again: each time it is another folder, of another inode.
  let made: Map<string, number>;
  let answer: (root: string) => Opened | Promise<Opened>;
  const folder = () => folders[ROOT_A] ?? "";
  const copyOf = (root: string): Copy => ({
    place: { key: KEY, history: join(base, "data", "history", KEY), real: { path: folder(), ...identities.get(folder())! } },
    folder: { path: join(base, "data", "history", KEY, "threads", root), dev: 7, ino: (root === ROOT_A ? 70 : 80) + (made.get(root) ?? 0), boot: BOOT_ID },
    at: folder(),
  });
  const handed = (root: string): Opened => ({ copy: copyOf(root), handle: Object.freeze({ root }) });
  const spawnHost = (behave: (host: FakeHost, message: ToHost) => void = guarding) => () => {
    const host = new FakeHost(behave);
    host.onExit(() => void (gone += 1));
    fakes.push(host);
    return host;
  };
  // Both roots are threads on the first root's folder.
  const threads = (overrides: Partial<ToolHostsOptions> = {}) => toolHosts({
    bindingOf: (root) => (root === ROOT_A || root === ROOT_B ? { folder: folder(), ...identities.get(folder())!, history: root } : undefined),
    copies: {
      // As Copies answers: a cancel at once, whatever the copy's making does.
      open: (root, _bound, aborted) => {
        opens.push(root);
        return Promise.race([
          Promise.resolve(answer(root)),
          new Promise<Opened>((resolve) => aborted.addEventListener("abort", () => resolve({ failed: CANCELLED }), { once: true })),
        ]);
      },
      close: (handle) => void closed.push({ handle, gone, released: [...released] }),
    },
    release: async (root) => void released.push(root),
    spawnHost: spawnHost(),
    ...overrides,
  });
  const works = (root: string) => ({ folder: copyOf(root).folder, at: folder() });

  beforeEach(() => {
    fakes = [];
    gone = 0;
    opens = [];
    closed = [];
    released = [];
    made = new Map();
    answer = handed;
  });

  it("starts its host on its copy once that is made, named by the folder's path, and two threads on one folder each have their own", async () => {
    const executor = threads();
    const [a, b] = [op("resolve", { path: "" }), op("resolve", { path: "" }, ROOT_B)];
    expect(await Promise.all([executor.run(a, signal()), executor.run(b, signal())])).toEqual([{ ok: a.id }, { ok: b.id }]);
    expect(opens).toEqual([ROOT_A, ROOT_B]);
    expect(fakes.map((host) => host.sent[0])).toEqual([ROOT_A, ROOT_B].map((root) => ({
      type: "start", folder: copyOf(root).folder.path, expect: { dev: 7, ino: copyOf(root).folder.ino, boot: BOOT_ID }, at: folder(),
      tmp: join(base, "data", "tmp", root), dataDir: join(base, "data"), cacheDir: join(base, "cache", "surogate"),
      env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8" }, appDirs: [...APP_DIRS, join(base, "tools")],
    })));
  });

  it("takes every kind of a thread's to the host on its copy, and gives what its commands run in the copy to share at the folder's path", async () => {
    const executor = threads();
    for (const kind of KINDS) expect(await executor.run(op(kind, { key: join(folder(), "a.txt") }), signal()), kind).toMatchObject({ ok: expect.any(String) });
    const shared: unknown[] = [];
    for (const guard of ["around", "before", null] as Guard[]) {
      expect(await executor.guarded(op("run", { command: "touch planted" }), signal(), guard, async (given) => {
        shared.push(given);
        return { ok: null };
      })).toEqual({ ok: null });
    }
    expect(shared).toEqual([works(ROOT_A), works(ROOT_A), works(ROOT_A)]);
    // One host, on the copy: every operation went to it, and none to a host on the folder.
    expect(fakes).toHaveLength(1);
    expect(fakes[0]?.sent[0]).toMatchObject({ type: "start", folder: copyOf(ROOT_A).folder.path, at: folder() });
    expect(fakes[0]?.sent.filter((message) => message.type === "op").map((message) => message.type === "op" && message.kind)).toEqual(KINDS);
  });

  it.each([
    ["the folder has no history", () => historyOff("cap", folder())],
    ["the folder is not the one bound", () => FOLDER_UNAVAILABLE],
    ["the copy could not be made", () => ({ error: { type: "unavailable", message: "This computer could not make this thread's copy of its folder: no KVM here" } })],
  ])("runs nothing for a thread whose copy is not there to work in, as where %s, and starts no host", async (_, failure: () => Outcome) => {
    answer = () => ({ failed: failure() });
    const executor = threads();
    for (const kind of KINDS) expect(await executor.run(op(kind, { key: join(folder(), "a.txt"), data: "" }), signal()), kind).toEqual(failure());
    let ran = 0;
    for (const guard of ["around", "before", null] as Guard[]) {
      expect(await executor.guarded(op("run", { command: "touch planted" }), signal(), guard, async () => {
        ran += 1;
        return { ok: null };
      })).toEqual(failure());
    }
    expect([ran, fakes.length, closed.length]).toEqual([0, 0, 0]);
  });

  it("answers a thread's operations unavailable where nothing makes copies, and never starts a host on its folder, where a chat still works", async () => {
    const executor = threads({
      // The first root a thread, the second a chat on the same folder itself.
      bindingOf: (root) => (root === ROOT_A || root === ROOT_B ? { folder: folder(), ...identities.get(folder())!, ...(root === ROOT_A ? { history: root } : {}) } : undefined),
      copies: undefined,
    });
    const refused = { error: { type: "unavailable", message: "This computer could not open the folder's sandbox: it has none to make this thread's copy of its folder in" } };
    let ran = 0;
    expect(await executor.run(op("write", { key: join(folder(), "a.txt"), data: "" }), signal())).toEqual(refused);
    expect(await executor.guarded(op("run", { command: "touch planted" }), signal(), "around", async () => {
      ran += 1;
      return { ok: null };
    })).toEqual(refused);
    expect([ran, fakes.length, executor.keepsCopies()]).toEqual([0, 0, false]);
    // The chat on the folder works in it as ever: its host holds the folder, and its commands share it.
    const shared: unknown[] = [];
    expect(await executor.guarded(op("run", { command: "true" }, ROOT_B), signal(), null, async (given) => {
      shared.push(given);
      return { ok: null };
    })).toEqual({ ok: null });
    expect(shared).toEqual([{ folder: { path: folder(), ...identities.get(folder()) } }]);
    expect(fakes[0]?.sent[0]).toMatchObject({ type: "start", folder: folder(), expect: identities.get(folder()) });
    expect(fakes[0]?.sent[0]).not.toHaveProperty("at");
    expect(threads().keepsCopies()).toBe(true);
  });

  it("starts one host for a root's operations that come together, asking for its copy once, and answers a cancel while the copy is made", async () => {
    let make: (opened: Opened) => void = () => {};
    answer = () => new Promise<Opened>((resolve) => {
      make = resolve;
    });
    const executor = threads();
    const cancel = new AbortController();
    const [a, b] = [op("resolve", { path: "" }), op("resolve", { path: "" })];
    const cancelled = executor.run(a, cancel.signal);
    const waiting = executor.run(b, signal());
    await until(() => opens.length === 1);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    expect(fakes).toHaveLength(0);
    make(handed(ROOT_A));
    expect(await waiting).toEqual({ ok: b.id });
    expect([opens, fakes.length]).toEqual([[ROOT_A], 1]);
  });

  it("lets its copy go once each host on it has stopped and its root's commands in the guest have ended, once a host", async () => {
    const executor = threads({ idleMs: 30 });
    await executor.run(op("resolve", { path: "" }), signal());
    await until(() => closed.length === 1);
    expect(closed).toEqual([{ handle: { root: ROOT_A }, gone: 1, released: [ROOT_A] }]);
    // The next host has its copy opened again, with a hold of its own, which goes as it stops at the app's quit.
    await executor.run(op("resolve", { path: "" }), signal());
    expect(opens).toEqual([ROOT_A, ROOT_A]);
    await executor.stop();
    await until(() => closed.length === 2);
    expect(closed[1]).toEqual({ handle: { root: ROOT_A }, gone: 2, released: [ROOT_A, ROOT_A] });
    expect(closed[1]?.handle).not.toBe(closed[0]?.handle);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(closed).toHaveLength(2);
  });

  it("lets a copy go only once its root's commands in the guest have ended, though its host died before", async () => {
    let tear = () => {};
    const executor = threads({
      release: (root) => new Promise<void>((resolve) => {
        tear = () => {
          released.push(root);
          resolve();
        };
      }),
    });
    await executor.run(op("resolve", { path: "" }), signal());
    fakes[0]?.exit();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([gone, closed]).toEqual([1, []]);
    tear();
    await until(() => closed.length === 1);
    expect(closed).toEqual([{ handle: { root: ROOT_A }, gone: 1, released: [ROOT_A] }]);
  });

  it("stops the hosts on a copy no longer vouched for before its root's next operation, which works in the copy opened then", async () => {
    // The guest lets go of what it shared for the first host when the test says.
    let tear = () => {};
    let asked = 0;
    const executor = threads({
      release: (root) => {
        asked += 1;
        if (asked > 1) return Promise.resolve(void released.push(root));
        return new Promise<void>((resolve) => {
          tear = () => {
            released.push(root);
            resolve();
          };
        });
      },
    });
    expect(await executor.run(op("resolve", { path: "" }), signal())).toMatchObject({ ok: expect.any(String) });
    // The guest made it again, or left it other than whole: the copy the host holds is no longer the thread's.
    executor.replaced(ROOT_A);
    made.set(ROOT_A, 1);
    const shared: unknown[] = [];
    const next = executor.guarded(op("run", { command: "ls" }), signal(), null, async (given) => {
      shared.push(given);
      return { ok: null };
    });
    // A host of its own on the copy the guest opened now; the one before has none of the root's operations.
    await until(() => fakes.length === 2);
    expect(fakes[1]?.sent[0]).toMatchObject({ type: "start", folder: copyOf(ROOT_A).folder.path, expect: { dev: 7, ino: 71, boot: BOOT_ID }, at: folder() });
    expect(fakes[0]?.count("op")).toBe(1);
    // What the root runs in the guest waits until the guest has let go of what it shared for the host before.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(shared).toEqual([]);
    tear();
    expect(await next).toEqual({ ok: null });
    expect(shared).toEqual([works(ROOT_A)]);
    await until(() => closed.length === 1);
    expect([fakes[0]?.count("stop"), closed[0]?.gone, opens]).toEqual([1, 1, [ROOT_A, ROOT_A]]);
  });

  it("stops a host it is told to stop once, whatever it is told after and whatever of its own ends meanwhile", async () => {
    let tear = () => {};
    let asked = 0;
    const executor = threads({
      idleMs: 20,
      // It answers its operations when the test says.
      spawnHost: spawnHost(readyOnly),
      release: (root) => {
        asked += 1;
        if (asked > 1) return Promise.resolve(void released.push(root));
        return new Promise<void>((resolve) => {
          tear = () => {
            released.push(root);
            resolve();
          };
        });
      },
    });
    const running = executor.run(op("resolve", { path: "" }), signal());
    await until(() => fakes[0]?.count("op") === 1);
    executor.replaced(ROOT_A);
    executor.replaced(ROOT_A);
    // Its operation ends while the guest lets go, and the host is idle past its time.
    const sent = fakes[0]?.sent.find((message) => message.type === "op");
    fakes[0]?.say({ type: "result", id: sent?.type === "op" ? sent.id : "", outcome: { ok: "done" } });
    expect(await running).toEqual({ ok: "done" });
    await new Promise((resolve) => setTimeout(resolve, 80));
    tear();
    await until(() => closed.length === 1);
    expect(fakes[0]?.count("stop")).toBe(1);
  });

  it("stops a host that finds another folder at its copy's path, and says so by the folder: the next operation has the copy opened again", async () => {
    // As host.ts answers an operation once its copy's path leads to another folder than the one it holds.
    const executor = threads({
      spawnHost: spawnHost((host, message) => {
        if (message.type === "op") host.say({ type: "result", id: message.id, outcome: FOLDER_UNAVAILABLE });
        else guarding(host, message);
      }),
    });
    const again = {
      error: {
        type: "unavailable",
        message: `This computer could not open the folder's sandbox: the copy of ${folder()} this thread works in was made again while this was asked, so it was not done. Ask again`,
      },
    };
    expect(await executor.run(op("read", { key: join(folder(), "a.txt"), max_bytes: null }), signal())).toEqual(again);
    await until(() => fakes[0]?.count("stop") === 1);
    // The same where what shares the copy in the guest finds it another.
    expect(await executor.guarded(op("run", { command: "ls" }), signal(), null, async () => FOLDER_UNAVAILABLE)).toEqual(again);
    await until(() => fakes[1]?.count("stop") === 1);
    expect([opens.length, fakes.length]).toEqual([2, 2]);
  });

  it("says by the folder, not as a folder gone, that a host found its copy made again as it started", async () => {
    const said = `the copy of ${folder()} this thread works in was made again after the app looked at it`;
    const executor = threads({
      spawnHost: spawnHost((host, message) => {
        if (message.type === "start") host.say({ type: "failed", message: said, folder: true });
        onStop(host, message);
      }),
    });
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ error: { type: "unavailable", message: `This computer could not open the folder's sandbox: ${said}` } });
  });

  it("runs no command of a host told to go once its letting go has begun, though its hook guard answers as it stops", async () => {
    // As host.ts answers a refusal it was asked before its stop: once the stop comes, and as it would have.
    const held: string[] = [];
    const executor = threads({
      spawnHost: spawnHost((host, message) => {
        if (message.type === "refusal") held.push(message.id);
        else if (message.type === "stop") {
          for (const id of held.splice(0)) host.say({ type: "result", id, outcome: { ok: null } });
          host.exit();
        } else guarding(host, message);
      }),
    });
    let ran = 0;
    const late = executor.guarded(op("run", { command: "touch late.txt" }), signal(), "around", async () => {
      ran += 1;
      return { ok: null };
    });
    await until(() => held.length === 1);
    executor.replaced(ROOT_A);
    expect(await late).toEqual({
      error: {
        type: "unavailable",
        message: `This computer could not open the folder's sandbox: the copy of ${folder()} this thread works in was made again while this was asked, so it was not done. Ask again`,
      },
    });
    expect([ran, released]).toEqual([0, [ROOT_A]]);
  });

  it("gives a replaced told in the moment between an open and its host to the host that open gave, which never starts on it", async () => {
    // As a letting go for another folder at the path tells the thread once the copies have taken its copy for it.
    let told = 0;
    const executor = threads({
      copies: {
        open: async (root) => {
          opens.push(root);
          const opened = handed(root);
          if (told === 0) {
            told += 1;
            queueMicrotask(() => executor.replaced(root));
          }
          return opened;
        },
        close: (handle) => void closed.push({ handle, gone, released: [...released] }),
      },
    });
    expect(await executor.run(op("resolve", { path: "" }), signal())).toMatchObject({ ok: expect.any(String) });
    // The copy the first open gave was let go at once, with no host on it; the host is on the copy opened after.
    expect([opens, fakes.length, closed]).toEqual([[ROOT_A, ROOT_A], 1, [{ handle: { root: ROOT_A }, gone: 0, released: [] }]]);
  });

  it("lets go of the hold an open gave where its host could not be made, and says why", async () => {
    const executor = threads({
      spawnHost: () => {
        throw new Error("spawn EMFILE");
      },
    });
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ error: { type: "unavailable", message: "This computer could not open the folder's sandbox: spawn EMFILE" } });
    expect(closed).toEqual([{ handle: { root: ROOT_A }, gone: 0, released: [] }]);
  });

  it("answers the app's quit while a thread's copy is made, and keeps no hold of what was made", async () => {
    let make: (opened: Opened) => void = () => {};
    const executor = threads({
      // Copies that make the copy whatever is asked meanwhile.
      copies: {
        open: (root) => {
          opens.push(root);
          return new Promise<Opened>((resolve) => {
            make = resolve;
          });
        },
        close: (handle) => void closed.push({ handle, gone, released: [...released] }),
      },
    });
    const running = executor.run(op("resolve", { path: "" }), signal());
    await until(() => opens.length === 1);
    const stopped = executor.stop();
    make(handed(ROOT_A));
    expect(await running).toEqual({ error: { type: "unavailable", message: "This computer could not open the folder's sandbox: the app is quitting" } });
    await stopped;
    expect([fakes.length, closed]).toEqual([0, [{ handle: { root: ROOT_A }, gone: 0, released: [] }]]);
  });
});

// The same with the app's own hosts, in their sandbox: two threads' file tools, each in its copy. The copies are
// made here as the guest's git leaves them, in the folder's place in the app's data.
describe("a thread's hosts in their sandbox", { timeout: 60_000 }, () => {
  const data64 = (text: string) => Buffer.from(text).toString("base64");
  let copies: Record<string, string>;
  const threads = () => {
    const folder = folders[ROOT_A] ?? "";
    const place = { key: keyOf(folder), history: join(base, "data", "history", keyOf(folder)), real: { path: folder, ...identities.get(folder)! } };
    return toolHosts({
      bindingOf: (root) => (root === ROOT_A || root === ROOT_B ? { folder, ...identities.get(folder)!, history: root } : undefined),
      copies: {
        open: async (root) => {
          const path = copies[root] ?? "";
          const { dev, ino } = statSync(path);
          return { copy: { place, folder: { path, dev, ino, boot: BOOT_ID }, at: folder }, handle: Object.freeze({ root }) };
        },
        close: () => {},
      },
    });
  };

  beforeEach(() => {
    const folder = folders[ROOT_A] ?? "";
    writeFileSync(join(folder, "a.txt"), "the folder's\n");
    copies = {};
    for (const root of [ROOT_A, ROOT_B]) {
      copies[root] = join(base, "data", "history", keyOf(folder), "threads", root);
      mkdirSync(copies[root] ?? "", { recursive: true });
      writeFileSync(join(copies[root] ?? "", "a.txt"), "the folder's\n");
    }
  });

  it("gives two threads on one folder a copy each to work in at once, named by the folder's path, and leaves the folder as it was", async () => {
    const executor = threads();
    const folder = folders[ROOT_A] ?? "";
    const before = asItLies(folder);
    const key = join(folder, "a.txt");
    expect(await Promise.all([executor.run(op("resolve", { path: "a.txt" }), signal()), executor.run(op("resolve", { path: "a.txt" }, ROOT_B), signal())])).toEqual([{ ok: key }, { ok: key }]);
    expect(await executor.run(op("write", { key, data: data64("A's\n") }), signal())).toEqual({ ok: null });
    expect(await executor.run(op("write", { key, data: data64("B's\n") }, ROOT_B), signal())).toEqual({ ok: null });
    expect(await executor.run(op("read", { key, max_bytes: null }), signal())).toEqual({ ok: data64("A's\n") });
    expect([ROOT_A, ROOT_B].map((root) => readFileSync(join(copies[root] ?? "", "a.txt"), "utf8"))).toEqual(["A's\n", "B's\n"]);
    // A thread's helper reaches neither the folder, nor the other thread's copy, nor the folder's history.
    const other = join(copies[ROOT_B] ?? "", "a.txt");
    expect(await executor.run(op("read", { key: other, max_bytes: null }), signal())).toMatchObject({ error: { type: "sandbox" } });
    symlinkSync(other, join(copies[ROOT_A] ?? "", "theirs.txt"));
    symlinkSync(key, join(copies[ROOT_A] ?? "", "real.txt"));
    for (const name of ["theirs.txt", "real.txt"]) {
      expect(await executor.run(op("read", { key: join(folder, name), max_bytes: null }), signal()), name).toMatchObject({ error: { type: "sandbox" } });
    }
    const search = (await executor.run(op("ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 }), signal())) as { ok: string };
    expect(search.ok.split("\n").filter(Boolean)).toEqual([key]);
    // A download its page started is saved in its copy, by the folder's names, as any write of the thread's.
    const staged = join(base, "staged.txt");
    writeFileSync(staged, "from a page\n");
    const save = downloadSaver({ get: (root) => (root === ROOT_A ? ({ folder } as Binding) : undefined) }, {
      admit: async () => null, run: (operation, aborted) => executor.run(operation, aborted),
    });
    expect(await save({ root: ROOT_A, session: ROOT_A, name: "page.txt", path: staged, user: false })).toBe(
      "The page downloaded \"page.txt\". It is saved in the chat's folder as Downloads/page.txt.",
    );
    expect(readFileSync(join(copies[ROOT_A] ?? "", "Downloads", "page.txt"), "utf8")).toBe("from a page\n");
    expect(asItLies(folder)).toEqual(before);
  });
});
