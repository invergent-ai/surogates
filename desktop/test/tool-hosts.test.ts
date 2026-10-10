import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, utimesSync,
  writeFileSync,
} from "node:fs";
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
import { LOCK_WAIT_MS } from "../src/hosts/folder-record.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type NetworkAnswer, type NetworkAsk, type ToHost } from "../src/hosts/messages.js";
import {
  APP_DIRS, type BoundFolder, CANCELLED, folderBusy, forkHost, type Guard, HOST_STOPPED, type HostProcess, LAND_WAIT_MS, NODE, NOT_BOUND, nothingToLand, QUIT_STEP_MS,
  type Recovery, RETIRE_STEP_MS, START_TIMEOUT_MS, type ThreadCopies, ToolHosts, type ToolHostsOptions,
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
// What the stand-ins for a folder's history answer the one thing the tool hosts ask of it, a landing's forgetting: the
// landing may go, each file it applied in the folder as it was before.
const forgets: ThreadCopies["ask"] = async () => ({ ok: { landing: null } });

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
      ask: forgets,
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
    // The app's data, where a folder's landings keep what they replace.
    mkdirSync(join(base, "data"));
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
        ask: forgets,
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
        ask: forgets,
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

  // A thread's landing writes the folder itself through a host of its own (spec, Section 13, "Landing on the computer").
  // A step of it, as the thread's turn asks it: under the turn's own name unless said.
  const landing = (action: string, more: Record<string, unknown> = {}, root = ROOT_A, invocationId = "land:7"): Operation => ({
    ...op("land", { action, ...more }, root), invocationId,
  });
  const NOT_BEGUN = {
    error: { type: "unavailable", message: "This computer could not open the folder's sandbox: the host of this landing was stopping, so this step of it was not begun. Ask again" },
  };

  it("lands through a host of its own on the folder itself, given its copy to read and the folder's kept folder, and asks that host no kind but the landing's", async () => {
    const executor = threads();
    await executor.run(op("resolve", { path: "" }), signal());
    const look = landing("revisions", { paths: ["a.txt"] });
    expect(await executor.land(look, signal())).toEqual({ ok: look.id });
    // Its thread's copy is opened for it too, with a hold of its own.
    expect([opens, fakes.length]).toEqual([[ROOT_A, ROOT_A], 2]);
    expect(fakes[1]?.sent[0]).toEqual({
      type: "start", folder: folder(), expect: identities.get(folder()),
      landing: { copy: copyOf(ROOT_A).folder.path, kept: join(base, "data", "landings", KEY) }, lockWaitMs: LAND_WAIT_MS,
      // A working folder of its own: the host on the copy runs beside it.
      tmp: join(base, "data", "tmp", `${ROOT_A}.land`), dataDir: join(base, "data"), cacheDir: join(base, "cache", "surogate"),
      env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8" }, appDirs: [...APP_DIRS, join(base, "tools")],
    });
    expect(fakes[1]?.sent.filter((message) => message.type === "op")).toEqual([{ type: "op", id: look.id, kind: "land", args: look.args }]);
    // The root's file kinds go to its host on the copy; the landing's host is asked no other.
    const write = op("write", { key: join(folder(), "a.txt"), data: "" });
    expect(await executor.run(write, signal())).toEqual({ ok: write.id });
    expect(fakes[0]?.sent.at(-1)).toMatchObject({ type: "op", id: write.id, kind: "write" });
    expect(await executor.land(op("write", { key: join(folder(), "a.txt"), data: "" }), signal())).toEqual({
      error: { type: "unsupported", message: "A landing's host cannot do 'write'" },
    });
    expect([fakes.length, fakes[1]?.count("op")]).toEqual([2, 1]);
  });

  it.each([
    ["the folder has no history", () => historyOff("cap", folder())],
    ["the folder is not the one bound", () => FOLDER_UNAVAILABLE],
    ["the copy could not be made", () => ({ error: { type: "unavailable", message: "This computer could not make this thread's copy of its folder: no KVM here" } })],
  ])("lands nothing for a thread whose copy is not there to land from, as where %s, and starts no host", async (_, failure: () => Outcome) => {
    answer = () => ({ failed: failure() });
    const executor = threads();
    for (const action of ["recover", "revisions", "apply", "unapply", "forget"]) {
      expect(await executor.land(landing(action, action === "forget" ? { saga: "s1", applied: [] } : {}), signal()), action).toEqual(failure());
    }
    expect([fakes.length, closed.length]).toEqual([0, 0]);
  });

  it("refuses a landing for a chat that works in its folder itself, by the folder's name, and lands nothing where nothing makes copies", async () => {
    const executor = threads({
      // The first root a thread, the second a chat on the same folder itself.
      bindingOf: (root) => (root === ROOT_A || root === ROOT_B ? { folder: folder(), ...identities.get(folder())!, ...(root === ROOT_A ? { history: root } : {}) } : undefined),
    });
    const chat = await executor.land(landing("revisions", { paths: [] }, ROOT_B), signal());
    expect(chat).toEqual(nothingToLand(folder()));
    expect(chat).toEqual({ error: { type: "unsupported", message: `This chat works in ${folder()} itself, so it has no copy of it to land from` } });
    expect(await executor.land(landing("revisions", { paths: [] }, "33333333-3333-4333-8333-333333333333"), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await threads({ copies: undefined }).land(landing("revisions", { paths: [] }), signal())).toEqual({
      error: { type: "unavailable", message: "This computer could not open the folder's sandbox: it has none to make this thread's copy of its folder in" },
    });
    expect([fakes.length, opens]).toEqual([0, []]);
  });

  it("answers a landing unavailable, by the folder's name, and starts no host, where the app's data that keeps what a landing replaces has gone", async () => {
    const executor = threads();
    rmSync(join(base, "data"), { recursive: true });
    expect(await executor.land(landing("revisions", { paths: [] }), signal())).toEqual({
      error: {
        type: "unavailable",
        message: `This computer could not open the folder's sandbox: the app's data, where a landing in ${folder()} keeps the files it replaces, is not there or is not the app's own`,
      },
    });
    // The hold its copy was opened with goes at once.
    expect([fakes.length, closed]).toEqual([0, [{ handle: { root: ROOT_A }, gone: 0, released: [] }]]);
  });

  it("holds the folder for the whole landing in one host, and lets it go at once when the landing is forgotten", async () => {
    const executor = threads();
    for (const step of [landing("revisions", { paths: ["a.txt"] }), landing("apply", { saga: "s1", step: 0, path: "a.txt" }), landing("unapply", { saga: "s1", step: 0, path: "a.txt" })]) {
      expect(await executor.land(step, signal())).toEqual({ ok: step.id });
    }
    expect([fakes.length, fakes[0]?.count("op"), fakes[0]?.count("stop")]).toEqual([1, 3, 0]);
    const forget = landing("forget", { saga: "s1", applied: [] });
    expect(await executor.land(forget, signal())).toEqual({ ok: forget.id });
    await until(() => gone === 1);
    expect(fakes[0]?.count("stop")).toBe(1);
    // It held its copy by a hold of its own, let go once it had stopped; nothing of it was in the guest to let go.
    expect(closed).toEqual([{ handle: { root: ROOT_A }, gone: 1, released: [] }]);
    // The next landing starts a host of its own.
    await executor.land(landing("revisions", { paths: [] }), signal());
    expect(fakes).toHaveLength(2);
  });

  it("keeps the folder held through the forgetting of a landing its turn only settled, and starts no host for a hold's forgetting where the thread holds nothing", async () => {
    const executor = threads();
    // Its turn gives back a hold it never took: nothing is let go, nothing waits for the folder, and its copy is not opened.
    expect(await executor.land(landing("forget", { saga: "hold:41", applied: [] }, ROOT_A, "land:41:release:9"), signal())).toEqual({ ok: {} });
    expect([fakes.length, opens]).toEqual([0, []]);
    await executor.land(landing("recover", {}, ROOT_A, "land:41:hold"), signal());
    // A landing another left running in the folder, settled first and forgotten under the settle's own name: the turn's
    // host goes on holding the folder, so no other landing begins before the turn's own.
    const settled = landing("forget", { saga: "saga:left-running", applied: [] }, ROOT_A, "land:41:settle:saga:left-running");
    expect(await executor.land(settled, signal())).toEqual({ ok: settled.id });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fakes[0]?.count("stop")).toBe(0);
    await executor.land(landing("revisions", { paths: ["a.txt"] }, ROOT_A, "land:41"), signal());
    expect(fakes).toHaveLength(1);
    // Its own landing forgotten, the folder is let go.
    expect(await executor.land(landing("forget", { saga: "saga:its-own", applied: [] }, ROOT_A, "land:41"), signal())).toMatchObject({ ok: expect.any(String) });
    await until(() => gone === 1);
    // So it is where the turn gives its hold back while it holds the folder.
    await executor.land(landing("recover", {}, ROOT_A, "land:42:hold"), signal());
    expect(await executor.land(landing("forget", { saga: "hold:42", applied: [] }, ROOT_A, "land:42:release:7"), signal())).toMatchObject({ ok: expect.any(String) });
    await until(() => gone === 2);
    expect(fakes.map((host) => host.count("stop"))).toEqual([1, 1]);
  });

  it("answers a landing busy, by the folder's name, where another held the folder for as long as it waits, and a chat's host as ever", async () => {
    const busy = "another chat on this computer is working in this folder; this one can use it once that one is done";
    const executor = threads({
      bindingOf: (root) => (root === ROOT_A || root === ROOT_B ? { folder: folder(), ...identities.get(folder())!, ...(root === ROOT_A ? { history: root } : {}) } : undefined),
      // As host.ts says it once its wait for the folder's lock ran out, and goes.
      spawnHost: spawnHost((host, message) => {
        if (message.type !== "start") return;
        host.say({ type: "failed", message: busy, busy: true });
        host.exit();
      }),
    });
    expect(await executor.land(landing("revisions", { paths: [] }), signal())).toEqual(folderBusy(folder()));
    expect(folderBusy(folder())).toEqual({
      error: { type: "busy", message: `Another chat on this computer is working in ${folder()}, so nothing of this landing was done. It can land once that one is done` },
    });
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual({ error: { type: "unavailable", message: `This computer could not open the folder's sandbox: ${busy}` } });
    // The landing's hold of the copy goes with the host that never started.
    expect(closed).toEqual([{ handle: { root: ROOT_A }, gone: 1, released: [] }]);
  });

  it("gives a landing's host its wait for the folder and a host's own time to start: its helper puts back what a landing cut short in a step, not in its start", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const executor = threads({ spawnHost: spawnHost(onStop) });
      let answered: unknown = null;
      void executor.land(landing("revisions", { paths: [] }), signal()).then((outcome) => {
        answered = outcome;
      });
      const bound = LAND_WAIT_MS + START_TIMEOUT_MS;
      await vi.advanceTimersByTimeAsync(bound - 1);
      expect([answered, fakes[0]?.killed]).toEqual([null, 0]);
      await vi.advanceTimersByTimeAsync(1);
      expect(answered).toEqual({ error: { type: "unavailable", message: `This computer could not open the folder's sandbox: its tool host did not start within ${bound / 1000} seconds` } });
      expect(fakes[0]?.killed).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("starts one landing's host for its steps that come together, opening its copy once, and answers a cancel while the copy is opened", async () => {
    let make: (opened: Opened) => void = () => {};
    answer = () => new Promise<Opened>((resolve) => {
      make = resolve;
    });
    const executor = threads();
    const cancel = new AbortController();
    const [a, b] = [landing("revisions", { paths: [] }), landing("revisions", { paths: ["a.txt"] })];
    const cancelled = executor.land(a, cancel.signal);
    const waiting = executor.land(b, signal());
    await until(() => opens.length === 1);
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    make(handed(ROOT_A));
    expect(await waiting).toEqual({ ok: b.id });
    expect([opens, fakes.length, fakes[0]?.count("op")]).toEqual([[ROOT_A], 1, 1]);
  });

  it("answers the app's quit while a landing's copy is opened for it, and keeps no hold of what was opened", async () => {
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
        ask: forgets,
      },
    });
    const waiting = executor.land(landing("revisions", { paths: [] }), signal());
    await until(() => opens.length === 1);
    const stopped = executor.stop();
    make(handed(ROOT_A));
    expect(await waiting).toEqual({ error: { type: "unavailable", message: "This computer could not open the folder's sandbox: the app is quitting" } });
    await stopped;
    expect(await executor.land(landing("revisions", { paths: [] }), signal())).toEqual(await waiting);
    expect([fakes.length, closed]).toEqual([0, [{ handle: { root: ROOT_A }, gone: 0, released: [] }]]);
  });

  it("holds its thread's copy for its landing by a hold of its own, through the host on the copy going, until the landing's host has stopped", async () => {
    const given: Handle[] = [];
    answer = (root) => {
      const opened = handed(root);
      if ("handle" in opened) given.push(opened.handle);
      return opened;
    };
    // The host on the copy answers; the landing's takes its step, and answers it when the test says.
    const executor = threads({ idleMs: 30, spawnHost: () => spawnHost(fakes.length === 0 ? guarding : readyOnly)() });
    await executor.run(op("resolve", { path: "" }), signal());
    const step = landing("apply", { saga: "s1", step: 0, path: "a.txt" });
    const applying = executor.land(step, signal());
    await until(() => fakes[1]?.count("op") === 1 && gone === 1);
    // The copy's host idled out and let its hold go; the landing's is kept while its step runs, past any idle time.
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(closed.map(({ handle }) => given.indexOf(handle))).toEqual([0]);
    fakes[1]?.say({ type: "result", id: step.id, outcome: { ok: "applied" } });
    expect(await applying).toEqual({ ok: "applied" });
    await until(() => closed.length === 2);
    expect([closed[1]?.handle === given[1], closed[1]?.gone]).toEqual([true, 2]);
  });

  it("keeps a landing's host, and the folder, when its thread's copy is made again under it: the landing goes on in that host, and the host on the copy goes", async () => {
    const executor = threads();
    await executor.run(op("resolve", { path: "" }), signal());
    await executor.land(landing("revisions", { paths: ["a.txt"] }), signal());
    executor.replaced(ROOT_A);
    made.set(ROOT_A, 1);
    await until(() => fakes[0]?.count("stop") === 1);
    const step = landing("apply", { saga: "s1", step: 0, path: "a.txt" });
    expect(await executor.land(step, signal())).toEqual({ ok: step.id });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([fakes.length, fakes[1]?.count("op"), fakes[1]?.count("stop")]).toEqual([2, 2, 0]);
    // The root's next file operation works in the copy opened now.
    await executor.run(op("resolve", { path: "" }), signal());
    expect(fakes[2]?.sent[0]).toMatchObject({ type: "start", folder: copyOf(ROOT_A).folder.path, expect: { dev: 7, ino: 71, boot: BOOT_ID } });
  });

  it.each([
    ["its thread deleted", (executor: ToolHosts) => executor.dismiss(ROOT_A)],
    ["this computer's access ended", (executor: ToolHosts) => executor.end()],
  ])("stops a landing's host, %s, only once the step its helper runs has ended, a cancelled one too, and the landing's next step waits in a host of its own", async (_, going) => {
    const executor = threads({ spawnHost: spawnHost(readyOnly) });
    const cancel = new AbortController();
    const step = landing("apply", { saga: "s1", step: 0, path: "a.txt" });
    const applying = executor.land(step, cancel.signal);
    await until(() => fakes[0]?.count("op") === 1);
    // Its caller is answered at once; its helper runs the step all the same.
    cancel.abort();
    expect(await applying).toEqual(CANCELLED);
    let over = false;
    const stopped = going(executor).then(() => {
      over = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([fakes[0]?.count("stop"), fakes[0]?.killed, over]).toEqual([0, 0, false]);
    const next = landing("revisions", { paths: [] });
    const looking = executor.land(next, signal());
    await until(() => fakes[1]?.count("op") === 1);
    expect(fakes[0]?.count("op")).toBe(1);
    fakes[0]?.say({ type: "result", id: step.id, outcome: { ok: "applied" } });
    await stopped;
    expect([fakes[0]?.count("stop"), fakes[0]?.killed]).toEqual([1, 0]);
    fakes[1]?.say({ type: "result", id: next.id, outcome: { ok: "looked" } });
    expect(await looking).toEqual({ ok: "looked" });
  });

  it("gives a landing's step QUIT_STEP_MS at the app's quit, and then stops its host: what the step left, the next landing's helper puts back", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const executor = threads({ spawnHost: spawnHost(readyOnly) });
      const [a, b] = [landing("apply", { saga: "s1", step: 0, path: "a.txt" }), landing("apply", { saga: "s2", step: 0, path: "b.txt" }, ROOT_B)];
      const [first, second] = [executor.land(a, signal()), executor.land(b, signal())];
      await vi.advanceTimersByTimeAsync(1);
      expect(fakes.map((host) => host.count("op"))).toEqual([1, 1]);
      let over = false;
      const quit = executor.stop().then(() => {
        over = true;
      });
      // A step that ends inside the bound: its host stops right after.
      await vi.advanceTimersByTimeAsync(QUIT_STEP_MS / 2);
      expect(fakes.map((host) => host.count("stop"))).toEqual([0, 0]);
      fakes[1]?.say({ type: "result", id: b.id, outcome: { ok: "applied" } });
      await vi.advanceTimersByTimeAsync(0);
      expect([fakes.map((host) => host.count("stop")), await second]).toEqual([[0, 1], { ok: "applied" }]);
      // One that has not ended by then is cut.
      await vi.advanceTimersByTimeAsync(QUIT_STEP_MS / 2 - 1);
      expect([fakes[0]?.count("stop"), over]).toEqual([0, false]);
      await vi.advanceTimersByTimeAsync(1);
      expect(fakes[0]?.count("stop")).toBe(1);
      await quit;
      expect(await first).toEqual(HOST_STOPPED);
    } finally {
      vi.useRealTimers();
    }
  });

  it("idles a landing's host out only once its helper has answered every step it was sent, a cancelled one too", async () => {
    const executor = threads({ idleMs: 30, spawnHost: spawnHost(readyOnly) });
    const cancel = new AbortController();
    const step = landing("apply", { saga: "s1", step: 0, path: "a.txt" });
    const applying = executor.land(step, cancel.signal);
    await until(() => fakes[0]?.count("op") === 1);
    cancel.abort();
    expect(await applying).toEqual(CANCELLED);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(fakes[0]?.count("stop")).toBe(0);
    // Past its idle time it still holds the folder for the landing: its next step goes to it.
    const next = landing("revisions", { paths: [] });
    const looking = executor.land(next, signal());
    await until(() => fakes[0]?.count("op") === 2);
    expect(fakes).toHaveLength(1);
    fakes[0]?.say({ type: "result", id: next.id, outcome: { ok: "looked" } });
    expect(await looking).toEqual({ ok: "looked" });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(fakes[0]?.count("stop")).toBe(0);
    // The cancelled step answered last: nothing of the landing runs, and the host idles out.
    fakes[0]?.say({ type: "result", id: step.id, outcome: { ok: "applied" } });
    await until(() => fakes[0]?.count("stop") === 1);
  });

  it("stops, when its thread is dismissed, the hosts its operations were starting while its copy was opened, once they are made", async () => {
    const makes: Array<(opened: Opened) => void> = [];
    answer = () => new Promise<Opened>((resolve) => void makes.push(resolve));
    // Hosts that are still starting when they are told to go.
    const executor = threads({ spawnHost: spawnHost(onStop) });
    const waiting = [executor.run(op("resolve", { path: "" }), signal()), executor.land(landing("revisions", { paths: [] }), signal())];
    await until(() => opens.length === 2);
    let over = false;
    const dismissed = executor.dismiss(ROOT_A).then(() => {
      over = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(over).toBe(false);
    for (const make of makes) make(handed(ROOT_A));
    await dismissed;
    // Each was told to go as it was made: neither ran what waited for it, and each let its hold go.
    expect([fakes.length, fakes.map((host) => host.count("stop")), fakes.map((host) => host.count("op")), closed.length]).toEqual([2, [1, 1], [0, 0], 2]);
    for (const outcome of await Promise.all(waiting)) {
      expect(outcome).toEqual({ error: { type: "unavailable", message: "This computer could not open the folder's sandbox: its tool host stopped while it was starting" } });
    }
  });

  // A root's operation as it reaches each of a thread's hosts: its host on the copy, or its landing's.
  const KINDS_OF_HOST: Array<[string, (executor: ToolHosts, aborted: AbortSignal) => Promise<Outcome>]> = [
    ["host on the copy", (executor, aborted) => executor.run(op("resolve", { path: "" }), aborted)],
    ["landing's host", (executor, aborted) => executor.land(landing("revisions", { paths: [] }), aborted)],
  ];
  const STOPPED_STARTING = { error: { type: "unavailable", message: "This computer could not open the folder's sandbox: its tool host stopped while it was starting" } };

  it.each(KINDS_OF_HOST)("starts no thread's %s for an operation cancelled while its copy was opened, and lets that copy's hold go", async (_, ask) => {
    const makes: Array<(opened: Opened) => void> = [];
    answer = () => new Promise<Opened>((resolve) => void makes.push(resolve));
    const executor = threads({ idleMs: 30 });
    const cancel = new AbortController();
    const asked = ask(executor, cancel.signal);
    await until(() => opens.length === 1);
    cancel.abort();
    expect(await asked).toEqual(CANCELLED);
    makes[0]?.(handed(ROOT_A));
    await new Promise((resolve) => setTimeout(resolve, 100));
    // Nothing holds the copy, nor the folder: no host was started, and the hold the copy was opened with goes.
    expect([fakes.length, closed]).toEqual([0, [{ handle: { root: ROOT_A }, gone: 0, released: [] }]]);
    // The root's next operation has its host, as ever.
    answer = handed;
    expect(await ask(executor, signal())).toMatchObject({ ok: expect.any(String) });
    expect(fakes).toHaveLength(1);
  });

  it.each(KINDS_OF_HOST)("answers an operation that comes to a thread's %s already cancelled at once, and opens it no copy and starts it no host", async (_, ask) => {
    const makes: Array<(opened: Opened) => void> = [];
    answer = () => new Promise<Opened>((resolve) => void makes.push(resolve));
    const executor = threads();
    const cancel = new AbortController();
    cancel.abort();
    expect(await ask(executor, cancel.signal)).toEqual(CANCELLED);
    for (const make of makes) make(handed(ROOT_A));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([opens.length, fakes.length, closed.length]).toEqual([0, 0, 0]);
  });

  it.each(KINDS_OF_HOST)("starts no thread's %s once this computer's access ended while its copy was opened, and an operation after has one of its own", async (_, ask) => {
    const makes: Array<(opened: Opened) => void> = [];
    answer = () => new Promise<Opened>((resolve) => void makes.push(resolve));
    const executor = threads();
    const first = ask(executor, signal());
    await until(() => opens.length === 1);
    const ended = executor.end();
    // Asked after the end, it waits for no start the end stopped: the copy is opened for it again.
    const second = ask(executor, signal());
    await until(() => opens.length === 2);
    for (const make of makes) make(handed(ROOT_A));
    expect(await first).toEqual(STOPPED_STARTING);
    expect(await second).toMatchObject({ ok: expect.any(String) });
    await ended;
    // The first start gave its hold back, and made no host; the one host is the second's, which holds its own.
    expect([fakes.length, closed.length]).toEqual([1, 1]);
  });

  it.each(KINDS_OF_HOST)("idles a thread's %s out only once it is ready, never while it starts, where the operation it was started for was cancelled meanwhile", async (_, ask) => {
    // Hosts slow to be ready, as a landing's is while its helper puts back what a step cut short.
    const executor = threads({
      idleMs: 30,
      spawnHost: spawnHost((host, message) => {
        if (message.type === "start") setTimeout(() => host.say({ type: "ready", processes: [] }), 200);
        else guarding(host, message);
      }),
    });
    const cancel = new AbortController();
    const asked = ask(executor, cancel.signal);
    await until(() => fakes.length === 1);
    cancel.abort();
    expect(await asked).toEqual(CANCELLED);
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(fakes[0]?.count("stop")).toBe(0);
    // Ready, with nothing to do, it goes after its idle time.
    await until(() => fakes[0]?.count("stop") === 1);
    expect(fakes).toHaveLength(1);
  });

  it("gives a hold given back while its landing's host is being started to that host, which then lets the folder go", async () => {
    const makes: Array<(opened: Opened) => void> = [];
    answer = () => new Promise<Opened>((resolve) => void makes.push(resolve));
    const executor = threads();
    const look = landing("revisions", { paths: [] }, ROOT_A, "land:41:hold");
    const looking = executor.land(look, signal());
    await until(() => opens.length === 1);
    // The turn gives its hold back while its copy is still opened for the host that will hold the folder.
    const release = landing("forget", { saga: "hold:41", applied: [] }, ROOT_A, "land:41:release:9");
    const releasing = executor.land(release, signal());
    makes[0]?.(handed(ROOT_A));
    expect(await Promise.all([looking, releasing])).toEqual([{ ok: look.id }, { ok: release.id }]);
    await until(() => fakes[0]?.count("stop") === 1);
    expect([fakes.length, opens.length]).toEqual([1, 1]);
  });

  // A thread's binding as the binder hands it to retired, before the journal forgets it.
  const boundA = (): BoundFolder => ({ folder: folder(), ...identities.get(folder())!, history: ROOT_A });

  it("gives a deleted thread's landing's host that wrote nothing in the folder RETIRE_STEP_MS for its step, then stops it, and answers the deletion at once", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const executor = threads({ spawnHost: spawnHost(readyOnly) });
      const look = landing("revisions", { paths: [] });
      const looking = executor.land(look, signal());
      await vi.advanceTimersByTimeAsync(1);
      expect(fakes[0]?.count("op")).toBe(1);
      expect(executor.retired(ROOT_A, boundA())).toBeUndefined();
      await vi.advanceTimersByTimeAsync(RETIRE_STEP_MS - 1);
      expect([fakes[0]?.count("stop"), executor.deletedLanding(ROOT_A)]).toEqual([0, undefined]);
      await vi.advanceTimersByTimeAsync(1);
      expect(fakes[0]?.count("stop")).toBe(1);
      expect(await looking).toEqual(HOST_STOPPED);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a deleted thread's landing that wrote in the folder in the host it has, past RETIRE_STEP_MS, starts it no other, and lets its binding go with that host", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      // The journal's binding until the binder retires the root, then none.
      let bound: BoundFolder | undefined = boundA();
      const live = threads({ bindingOf: (root) => (root === ROOT_A ? bound : undefined), idleMs: RETIRE_STEP_MS * 4 });
      for (const action of ["revisions", "unapply"]) {
        const step = landing(action, { saga: "s1", step: 0, path: "a.txt", paths: [] });
        const asked = live.land(step, signal());
        await vi.advanceTimersByTimeAsync(1);
        expect(await asked).toEqual({ ok: step.id });
      }
      live.retired(ROOT_A, boundA());
      bound = undefined;
      await vi.advanceTimersByTimeAsync(RETIRE_STEP_MS * 2);
      expect([fakes.length, fakes[0]?.count("stop"), live.deletedLanding(ROOT_A)]).toEqual([1, 0, boundA()]);
      // Its put-back still reaches the host it kept.
      const back = landing("unapply", { saga: "s1", step: 0, path: "a.txt" });
      const putting = live.land(back, signal());
      await vi.advanceTimersByTimeAsync(1);
      expect(await putting).toEqual({ ok: back.id });
      // Idled out, as any landing's host: the deleted thread's binding goes with it, and no host is started for it after.
      await vi.advanceTimersByTimeAsync(RETIRE_STEP_MS * 4);
      expect([fakes[0]?.count("stop"), live.deletedLanding(ROOT_A)]).toEqual([1, undefined]);
      expect(await live.land(landing("recover"), signal())).toEqual(FOLDER_UNAVAILABLE);
      expect(fakes).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("starts a deleted thread no landing's host while the one it kept is stopping, its landing forgotten", async () => {
    let bound: BoundFolder | undefined = boundA();
    const executor = threads({
      bindingOf: (root) => (root === ROOT_A ? bound : undefined),
      // Hosts that take a while to go once told to stop.
      spawnHost: spawnHost((host, message) => {
        if (message.type === "stop") setTimeout(() => host.exit(), 100);
        else answering(host, message);
      }),
    });
    const apply = landing("apply", { saga: "s1", step: 0, path: "a.txt" });
    expect(await executor.land(apply, signal())).toEqual({ ok: apply.id });
    executor.retired(ROOT_A, boundA());
    bound = undefined;
    const forget = landing("forget", { saga: "s1", applied: [{ step: 0, path: "a.txt", before: null, after: "c".repeat(40) }] });
    expect(await executor.land(forget, signal())).toEqual({ ok: forget.id });
    // Its host is stopping, and is the landing's no more: nothing more of the deleted thread's is taken, nor a host started for it.
    expect(await executor.land(landing("recover"), signal())).toEqual(FOLDER_UNAVAILABLE);
    await until(() => gone === 1);
    expect([fakes.length, executor.deletedLanding(ROOT_A)]).toEqual([1, undefined]);
  });

  it.each([
    ["its thread deleted", (executor: ToolHosts) => executor.dismiss(ROOT_A)],
    ["the app quitting", (executor: ToolHosts) => executor.stop()],
  ])("begins no step in a landing's host told to go, %s, while the step waited for it to start, whatever it says as it stops", async (_, going) => {
    // As a host that has just taken the folder when it is told to stop: it says it is ready, then goes.
    const executor = threads({
      spawnHost: spawnHost((host, message) => {
        if (message.type !== "stop") return;
        host.say({ type: "ready", processes: [] });
        setTimeout(() => host.exit(), 20);
      }),
    });
    const waiting = executor.land(landing("revisions", { paths: [] }), signal());
    await until(() => fakes.length === 1);
    await going(executor);
    expect(await waiting).toEqual(NOT_BEGUN);
    expect(fakes[0]?.count("op")).toBe(0);
  });
});

// The same with the app's own hosts, in their sandbox: two threads' file tools, each in its copy. The copies are
// made here as the guest's git leaves them, in the folder's place in the app's data.
describe("what landings cut short in this computer's folders, put back by a host of its own on each", { timeout: 5_000 }, () => {
  let fakes: FakeHost[];
  let told: Recovery[];
  // What each recovery's host says once it is started, by how many recoveries were started before it: ready, or why not.
  let says: (nth: number) => FromHost | null;
  // Whether a recovery's helper answers its put-back at once; one held is answered when the test says.
  let holds: boolean;
  const found = { restored: ["a.txt"], beside: [], lost: [], unread: [] };
  const recoveries = () => fakes.filter((host) => (host.sent[0] as HostStart).recovery !== undefined);
  // A recovery's host says what *says* has it say, and its helper answers its put-back; any other host is a chat's, which answers.
  const behave = (host: FakeHost, message: ToHost) => {
    const recovery = (host.sent[0] as HostStart).recovery !== undefined;
    if (message.type === "start" && recovery) {
      const said = says(recoveries().indexOf(host));
      if (said) host.say(said);
      if (said?.type === "failed") host.exit();
    } else if (message.type === "op" && recovery) {
      if (!holds) host.say({ type: "result", id: message.id, outcome: { ok: found } });
    } else {
      answering(host, message);
    }
    onStop(host, message);
  };
  const data = () => join(base, "data");
  const folder = () => folders[ROOT_A] ?? "";
  // What a folder's landings keep, holding something, and its place's record as the app writes it (history/place.ts), under *name*.
  const left = (at = folder(), name = keyOf(at), record: string | null = null) => {
    const { dev, ino, boot } = identities.get(at)!;
    mkdirSync(join(data(), "history"), { recursive: true });
    writeFileSync(join(data(), "history", `${name}.json`), record ?? JSON.stringify({ path: at, dev: String(dev), ino: String(ino), boot }));
    const kept = join(data(), "landings", name);
    mkdirSync(join(kept, "s1"), { recursive: true });
    writeFileSync(join(kept, "s1", "0.json"), "a step's record");
    return kept;
  };
  const recovering = (overrides: Partial<ToolHostsOptions> = {}) => toolHosts({
    spawnHost: () => {
      const host = new FakeHost(behave);
      host.onExit(() => void (exits += 1));
      fakes.push(host);
      return host;
    },
    recovered: (recovery) => void told.push(recovery),
    ...overrides,
  });
  const ready: FromHost = { type: "ready", processes: [] };
  const BUSY: FromHost = { type: "failed", message: "another chat on this computer is working in this folder; this one can use it once that one is done", busy: true };

  beforeEach(() => {
    fakes = [];
    told = [];
    says = () => ready;
    holds = false;
  });

  it("starts, as this computer's tools start, a recovery's host on each folder whose landings keep anything, on the folder its place's record names, asks it to put back what was cut short there, and lets the folder go", async () => {
    const kept = left();
    // What another folder's landings keep holds nothing: no host is started for it.
    mkdirSync(join(data(), "landings", keyOf(folders[ROOT_B] ?? "")), { recursive: true });
    const executor = recovering();
    executor.recoverLeft();
    await until(() => recoveries()[0]?.count("stop") === 1);
    expect(fakes).toHaveLength(1);
    expect(fakes[0]?.sent[0]).toEqual({
      type: "start", folder: folder(), expect: identities.get(folder()), recovery: { kept }, lockWaitMs: LAND_WAIT_MS,
      // A working folder of its own, by the folder's key.
      tmp: join(data(), "tmp", `${keyOf(folder())}.recover`), dataDir: data(), cacheDir: join(base, "cache", "surogate"),
      env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8" }, appDirs: [...APP_DIRS, join(base, "tools")],
    });
    expect(fakes[0]?.sent.filter((message) => message.type === "op")).toEqual([{ type: "op", id: expect.any(String), kind: "land", args: { action: "recover" } }]);
    // What it found, for whoever shows it: said as it begins and as it ends, and kept.
    expect(told).toEqual([{ folder: folder(), state: "begun" }, { folder: folder(), state: "found", found }]);
    expect(executor.recoveries()).toEqual([{ folder: folder(), state: "found", found }]);
  });

  it("puts back what a place set aside since kept, where its record names the folder; and says, and leaves, what is kept where no record says whose it is", async () => {
    const aside = left(folder(), `${keyOf(folder())}.was-1700000000000`);
    // Kept with no record, with one that cannot be read, and with one that names a folder of another key.
    const other = folders[ROOT_B] ?? "";
    mkdirSync(join(data(), "landings", keyOf(other), "s1"), { recursive: true });
    left(other, `${keyOf(other)}.was-1`, "{ not a record");
    left(other, `${keyOf(other)}.was-2`, JSON.stringify({ path: folder(), dev: "1", ino: "2", boot: "" }));
    // And a name that is no folder's under where every folder's is kept.
    mkdirSync(join(data(), "landings", "not a key", "s1"), { recursive: true });
    const before = asItLies(join(data(), "landings"));
    const executor = recovering();
    executor.recoverLeft();
    await until(() => told.length === 5);
    expect(recoveries().map((host) => (host.sent[0] as HostStart).recovery)).toEqual([{ kept: aside }]);
    const why = "The app's record of which folder a landing on this computer was cut short in cannot be read, so what that landing kept is left as it is";
    expect(executor.recoveries()).toEqual(expect.arrayContaining([
      { folder: folder(), state: "found", found },
      ...[1, 2, 3].map(() => ({ folder: null, state: "left", why })),
    ]));
    expect(executor.recoveries()).toHaveLength(4);
    await until(() => recoveries()[0]?.count("stop") === 1);
    expect(asItLies(join(data(), "landings"))).toEqual(before);
  });

  it("says a folder that is not there, or is not the one recorded, as its host does, and puts nothing back there", async () => {
    left();
    says = () => ({ type: "failed", message: `the folder ${folder()} is not there`, folder: true });
    const executor = recovering();
    executor.recoverLeft();
    await until(() => told.length === 2);
    expect(executor.recoveries()).toEqual([{ folder: folder(), state: "left", why: `the folder ${folder()} is not there` }]);
    expect(recoveries()[0]?.count("op")).toBe(0);
  });

  it("makes a recovery that meets a chat holding the folder wait, says it is busy once it has waited in vain, and asks it again once that chat's host has gone", async () => {
    left();
    says = (nth) => (nth === 0 ? BUSY : ready);
    const executor = recovering({ idleMs: 100 });
    expect(await executor.run(op("resolve", { path: "" }), signal())).toMatchObject({ ok: expect.any(String) });
    executor.recoverLeft();
    await until(() => told.length === 2);
    expect(told[1]).toEqual({
      folder: folder(), state: "left",
      why: `Another chat on this computer is working in ${folder()}, so what a landing cut short there is not put back yet. It is put back once that one is done`,
    });
    // The chat's host idles out, and lets the folder go: the recovery is asked again, and puts back what was cut short.
    await until(() => told.length === 4, 3_000);
    expect(told.slice(2)).toEqual([{ folder: folder(), state: "begun" }, { folder: folder(), state: "found", found }]);
    expect(recoveries()).toHaveLength(2);
  });

  it("starts a chat's host on a folder only once that folder's recovery holds it, and waits for that no longer than a chat waits for its folder", async () => {
    left();
    let go = () => {};
    says = (nth) => {
      if (nth > 0) return null;
      void new Promise<void>((resolve) => {
        go = resolve;
      }).then(() => recoveries()[0]?.say(ready));
      return null;
    };
    const chats = () => fakes.filter((host) => (host.sent[0] as HostStart).recovery === undefined);
    const executor = recovering();
    executor.recoverLeft();
    const chat = executor.run(op("resolve", { path: "" }), signal());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([recoveries().length, chats().length]).toEqual([1, 0]);
    go();
    expect(await chat).toMatchObject({ ok: expect.any(String) });
    expect(chats()).toHaveLength(1);
    // A recovery whose host never holds the folder keeps a chat from it for as long as a chat waits for a folder, and no longer.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const later = recovering();
      left(folders[ROOT_B] ?? "");
      later.recoverLeft();
      const waiting = later.run(op("resolve", { path: "" }, ROOT_B), signal());
      await vi.advanceTimersByTimeAsync(LOCK_WAIT_MS - 1);
      expect(chats()).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(await waiting).toMatchObject({ ok: expect.any(String) });
      expect(chats()).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("never stops a recovery's host inside its put-back: this computer's access ending waits for it, and the app's quit gives it QUIT_STEP_MS", async () => {
    left();
    holds = true;
    const executor = recovering();
    executor.recoverLeft();
    await until(() => recoveries()[0]?.count("op") === 1);
    let over = false;
    const ended = executor.end().then(() => {
      over = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect([recoveries()[0]?.count("stop"), recoveries()[0]?.killed, over]).toEqual([0, 0, false]);
    const step = recoveries()[0]?.sent.find((message) => message.type === "op") as Extract<ToHost, { type: "op" }>;
    recoveries()[0]?.say({ type: "result", id: step.id, outcome: { ok: found } });
    await ended;
    expect(recoveries()[0]?.count("stop")).toBeGreaterThan(0);
    // The quit: past its bound, the put-back is cut, and goes on from its record at the next start.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      executor.recoverLeft();
      await vi.advanceTimersByTimeAsync(1);
      expect(recoveries()[1]?.count("op")).toBe(1);
      const quit = executor.stop();
      await vi.advanceTimersByTimeAsync(QUIT_STEP_MS - 1);
      expect(recoveries()[1]?.count("stop")).toBe(0);
      await vi.advanceTimersByTimeAsync(1);
      expect(recoveries()[1]?.count("stop")).toBeGreaterThan(0);
      await quit;
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a thread's hosts in their sandbox", { timeout: 60_000 }, () => {
  const data64 = (text: string) => Buffer.from(text).toString("base64");
  let copies: Record<string, string>;
  // A chat on the threads' folder itself.
  const CHAT = "33333333-3333-4333-8333-333333333333";
  // The threads' copies, as the copies give them once the guest has made them.
  const copiesOf = (): ThreadCopies => {
    const folder = folders[ROOT_A] ?? "";
    const place = { key: keyOf(folder), history: join(base, "data", "history", keyOf(folder)), real: { path: folder, ...identities.get(folder)! } };
    return {
      open: async (root) => {
        const path = copies[root] ?? "";
        const { dev, ino } = statSync(path);
        return { copy: { place, folder: { path, dev, ino, boot: BOOT_ID }, at: folder }, handle: Object.freeze({ root }) };
      },
      close: () => {},
      ask: forgets,
    };
  };
  const threads = (overrides: Partial<ToolHostsOptions> = {}) => {
    const folder = folders[ROOT_A] ?? "";
    return toolHosts({
      bindingOf: (root) => {
        if (root === CHAT) return { folder, ...identities.get(folder)! };
        return root === ROOT_A || root === ROOT_B ? { folder, ...identities.get(folder)!, history: root } : undefined;
      },
      copies: copiesOf(),
      ...overrides,
    });
  };
  // A file's blob id, as git names its bytes, and the git blob a landing is told to write.
  const blob = (text: string) => createHash("sha1").update(`blob ${Buffer.byteLength(text)}\0`).update(text).digest("hex");
  // The folder as a person sees it: each name's mode, and a file's size, time and bytes. Not when each name last
  // changed in its folder, which a file taking its name back changes, nor a folder's own times, which change with what is put in it.
  const lies = (path: string) => Object.fromEntries(Object.entries(asItLies(path)).map(([name, found]) => {
    const { mode, size, mtime, bytes } = found as { mode: number; size: number; mtime: number; bytes?: string };
    return [name, bytes === undefined ? { mode } : { mode, size, mtime, bytes }];
  }));
  const sha256 = (text: string) => createHash("sha256").update(text).digest("hex");
  // What a landing's helper is left to do once it is told: for a test that stops it inside a step, the folder it
  // looks for the word in, which only that helper's sandbox and this test can both reach.
  const told = (root = ROOT_A) => join(base, "data", "tmp", `${root}.land`);
  // Loaded into a landing's file helper before its own code: at the first link that gives a.txt its name, it is held
  // there until the test says go, or ends there as a kill does. In an apply, that is the second of its two renames,
  // the user's file moved aside and its name empty; in a helper's first step, it is its put-back of what a step cut
  // short left. Once a helper, and for at most 20 s.
  const BETWEEN = `data:text/javascript,${encodeURIComponent(`
    import fs from "node:fs";
    import { syncBuiltinESMExports } from "node:module";
    const { LAND_AT: at, LAND_DOES: does, LAND_TOLD: told } = process.env;
    const real = fs.linkSync;
    let once = false;
    fs.linkSync = (...args) => {
      if (!once && new RegExp(at).test(String(args[1]))) {
        once = true;
        if (does === "kill") {
          process.kill(process.pid, "SIGKILL");
          for (;;);
        }
        fs.writeFileSync(told + "/held", "");
        const nap = new Int32Array(new SharedArrayBuffer(4));
        for (let waited = 0; !fs.existsSync(told + "/go") && waited < 20000; waited += 10) Atomics.wait(nap, 0, 0, 10);
      }
      return real(...args);
    };
    syncBuiltinESMExports();
  `)}`;
  // The next landing's helper a host started by upset() starts is held at that link, or killed there.
  const arm = (does: "hold" | "kill") => writeFileSync(join(base, "armed"), does);
  // Hosts loaded, before their own code, with what gives the next landing's helper that one is armed for the code
  // above as they start it; a recovery's helper too. *where*: the folder that helper says it is held in, one its sandbox writes.
  const upset = (where = told()) => {
    const armed = join(base, "armed");
    const helper = { NODE_OPTIONS: `--import=${BETWEEN}`, LAND_AT: "/a\\.txt$", LAND_TOLD: where };
    const hook = `data:text/javascript,${encodeURIComponent(`
      import cp from "node:child_process";
      import fs from "node:fs";
      import { syncBuiltinESMExports } from "node:module";
      const spawn = cp.spawn;
      cp.spawn = (...args) => {
        const landing = Array.isArray(args[1]) && String(args[1].at(-1)).includes("/files/helper.js") && args[2]?.env?.SUROGATE_KEPT;
        if (landing && fs.existsSync(${JSON.stringify(armed)})) {
          const does = fs.readFileSync(${JSON.stringify(armed)}, "utf8");
          fs.unlinkSync(${JSON.stringify(armed)});
          Object.assign(args[2].env, ${JSON.stringify(helper)}, { LAND_DOES: does });
        }
        return spawn(...args);
      };
      syncBuiltinESMExports();
    `).replaceAll("'", "%27")}`;
    const node = join(base, "upset-node");
    writeFileSync(node, `#!/bin/sh\nexec '${process.execPath}' --import '${hook}' "$@"\n`, { mode: 0o755 });
    return () => {
      const host = forkHost({ execPath: node });
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return host;
    };
  };
  const step = (action: string, more: Record<string, unknown> = {}, root = ROOT_A): Operation => ({ ...op("land", { action, ...more }, root), invocationId: "land:7" });

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

  // The thread changed a.txt in its copy; the user's own is in the folder, with a mode and a time of its own.
  const changed = () => {
    const folder = folders[ROOT_A] ?? "";
    writeFileSync(join(folder, "a.txt"), "the user's own\n");
    chmodSync(join(folder, "a.txt"), 0o640);
    utimesSync(join(folder, "a.txt"), 1_700_000_000, 1_700_000_000);
    writeFileSync(join(copies[ROOT_A] ?? "", "a.txt"), "the thread's\n");
    return folder;
  };
  // Its landing's look at a.txt, and the apply of the thread's over what it saw.
  const looked = async (executor: ToolHosts): Promise<Operation> => {
    const look = await executor.land(step("revisions", { paths: ["a.txt"] }), signal()) as { ok: { revisions: Array<[string, string]> } };
    const expected = look.ok.revisions[0]?.[1];
    return step("apply", { saga: "s1", step: 0, path: "a.txt", before: blob("the user's own\n"), after: blob("the thread's\n"), expected });
  };
  const applied = { ok: { path: "a.txt", before: blob("the user's own\n"), after: blob("the thread's\n"), made: [] } };
  // The folder once the thread's a.txt has landed whole over the user's: its bytes at the name, with the user's file's
  // mode, and nothing else changed or left beside it; and the user's file kept, whole, where the folder's landings keep theirs.
  const landed = (folder: string, before: ReturnType<typeof lies>) => {
    expect(lies(folder)).toEqual({ ...before, "a.txt": { mode: before["a.txt"]?.mode, size: 13, mtime: expect.any(Number), bytes: sha256("the thread's\n") } });
    expect(readFileSync(join(base, "data", "landings", keyOf(folder), "s1", "0"), "utf8")).toBe("the user's own\n");
  };

  it("lands a thread's file in the folder through the landing's host, which holds the folder until the landing is forgotten and holds up no host on a copy", { timeout: 90_000 }, async () => {
    const folder = changed();
    const executor = threads({ landWaitMs: 1_000 });
    const before = lies(folder);
    const apply = await looked(executor);
    expect(lies(folder)).toEqual(before);
    // A chat on the folder itself waits for it as long as a chat does, and is told; no host waits for ever.
    const chat = executor.run(op("resolve", { path: "" }, CHAT), signal());
    // The threads' hosts on their copies work meanwhile: each holds its copy, not the folder.
    for (const root of [ROOT_A, ROOT_B]) {
      expect(await executor.run(op("write", { key: join(folder, "b.txt"), data: data64(`${root}'s\n`) }, root), signal())).toEqual({ ok: null });
      expect(readFileSync(join(copies[root] ?? "", "b.txt"), "utf8")).toBe(`${root}'s\n`);
    }
    // A second landing in the folder waits its time, and is told by the folder's name that nothing of it was done.
    expect(await executor.land(step("revisions", { paths: [] }, ROOT_B), signal())).toEqual(folderBusy(folder));
    expect(lies(folder)).toEqual(before);
    expect(await executor.land(apply, signal())).toEqual(applied);
    landed(folder, before);
    expect(await chat).toEqual({
      error: { type: "unavailable", message: "This computer could not open the folder's sandbox: another chat on this computer is working in this folder; this one can use it once that one is done" },
    });
    const after = lies(folder);
    // Recorded, as the folder's history says: the landing names the apply it sent, and what it kept goes.
    const { ok: { path, before: from, after: to } } = applied;
    expect(await executor.land(step("forget", { saga: "s1", applied: [{ step: 0, path, before: from, after: to }] }), signal())).toEqual({ ok: {} });
    // Forgotten, the landing lets the folder go, and what it kept goes. The chat's host and the second landing's that
    // were told have gone, and the hosts on the copies stay.
    await until(() => exits === 3, 15_000);
    expect(existsSync(join(base, "data", "landings", keyOf(folder), "s1"))).toBe(false);
    // The second landing has the folder now, and the chat once that one is forgotten too; the folder is as it landed.
    expect(await executor.land(step("revisions", { paths: [] }, ROOT_B), signal())).toEqual({ ok: { revisions: [] } });
    expect(await executor.land(step("forget", { saga: "s2", applied: [] }, ROOT_B), signal())).toEqual({ ok: {} });
    await until(() => exits === 4, 15_000);
    expect(await executor.run(op("resolve", { path: "" }, CHAT), signal())).toEqual({ ok: folder });
    expect(lies(folder)).toEqual(after);
  });

  it("forgets nothing a landing's helper kept where the forgetting leaves out an apply it holds a record of, and forgets it once named", async () => {
    const folder = changed();
    const executor = threads();
    const apply = await looked(executor);
    expect(await executor.land(apply, signal())).toEqual(applied);
    const keeps = join(base, "data", "landings", keyOf(folder), "s1");
    const { ok: { path, before: from, after: to } } = applied;
    expect(await executor.land(step("forget", { saga: "s1", applied: [] }), signal())).toEqual({
      error: { type: "conflict", message: "Step 0 of this landing, of a.txt, was applied on this computer, and the steps named to forget the landing leave it out, so nothing the landing kept was forgotten" },
    });
    expect(await executor.land(step("forget", { saga: "s1", applied: [{ step: 0, path: "b.txt", before: from, after: to }] }), signal())).toMatchObject({ error: { type: "conflict" } });
    expect(readFileSync(join(keeps, "0"), "utf8")).toBe("the user's own\n");
    expect(await executor.land(step("forget", { saga: "s1", applied: [{ step: 0, path, before: from, after: to }] }), signal())).toEqual({ ok: {} });
    expect(existsSync(keeps)).toBe(false);
  });

  it.each([
    ["its thread deleted", (executor: ToolHosts) => executor.dismiss(ROOT_A)],
    ["the app quitting", (executor: ToolHosts) => executor.stop()],
  ])("leaves the file whole where a landing's host is told to go, %s, inside an apply: the step ends first, and then the host", async (_, going) => {
    const folder = changed();
    const executor = threads({ spawnHost: upset() });
    arm("hold");
    const before = lies(folder);
    const apply = await looked(executor);
    const applying = executor.land(apply, signal());
    await until(() => existsSync(join(told(), "held")), 20_000);
    // Inside it, between its two renames: the user's file moved aside, its name empty.
    expect(existsSync(join(folder, "a.txt"))).toBe(false);
    let over = false;
    const gone = going(executor).then(() => {
      over = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect([over, exits]).toEqual([false, 0]);
    writeFileSync(join(told(), "go"), "");
    expect(await applying).toEqual(applied);
    await gone;
    expect(exits).toBe(1);
    landed(folder, before);
  });

  it("puts back the user's file at its name, in the first step of the next landing's host, after a helper killed between an apply's two renames", async () => {
    const folder = changed();
    const executor = threads({ spawnHost: upset() });
    arm("kill");
    const before = lies(folder);
    const was = lstatSync(join(folder, "a.txt")).ino;
    expect(await executor.land(await looked(executor), signal())).toEqual(HOST_STOPPED);
    await until(() => exits === 1);
    // The name is empty, and the user's file lies beside it under a name of the landing's own.
    const beside = readdirSync(folder).filter((name) => name.startsWith(".surogate-"));
    expect([existsSync(join(folder, "a.txt")), beside.length, beside.some((name) => lstatSync(join(folder, name)).ino === was)]).toEqual([false, 2, true]);
    // The next landing's host puts it back in its first step, and says so.
    expect(await executor.land(step("recover"), signal())).toEqual({ ok: { restored: ["a.txt"], beside: [], lost: [], unread: [] } });
    expect(lies(folder)).toEqual(before);
    expect(lstatSync(join(folder, "a.txt")).ino).toBe(was);
  });

  it("puts back what a killed landing left, in the next landing's host, however long past its idle time that takes with no step waiting, and only then idles it out", async () => {
    const folder = changed();
    const executor = threads({ idleMs: 500, spawnHost: upset() });
    arm("kill");
    const before = lies(folder);
    expect(await executor.land(await looked(executor), signal())).toEqual(HOST_STOPPED);
    await until(() => exits === 1);
    // The next landing's helper is held inside its put-back, in the step its host was started for; and that step is
    // cancelled meanwhile.
    arm("hold");
    const cancel = new AbortController();
    const recovering = executor.land(step("recover"), cancel.signal);
    await until(() => existsSync(join(told(), "held")), 20_000);
    cancel.abort();
    expect(await recovering).toEqual(CANCELLED);
    // Three idle times later it is still putting the file back: nothing stops it in there.
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    expect([exits, existsSync(join(folder, "a.txt"))]).toEqual([1, false]);
    writeFileSync(join(told(), "go"), "");
    // Its step answered, with no other waiting, it idles out; the user's file is back at its name, and a chat on the folder is served.
    await until(() => exits === 2, 15_000);
    expect(lies(folder)).toEqual(before);
    expect(await executor.run(op("resolve", { path: "" }, CHAT), signal())).toEqual({ ok: folder });
  });

  // The folder's place's record, as the app writes it when a thread's copy of the folder is first made (history/place.ts).
  const recorded = (folder: string) => {
    const { dev, ino, boot } = identities.get(folder)!;
    writeFileSync(join(base, "data", "history", `${keyOf(folder)}.json`), JSON.stringify({ path: folder, dev: String(dev), ino: String(ino), boot }));
  };
  // A landing's helper killed between an apply's two renames over the user's a.txt, and this computer's tools stopped
  // after: the folder as it was before the landing, and the user's file's inode.
  const killed = async (): Promise<{ folder: string; before: ReturnType<typeof lies>; was: number }> => {
    const folder = changed();
    recorded(folder);
    const executor = threads({ spawnHost: upset() });
    arm("kill");
    const before = lies(folder);
    const was = lstatSync(join(folder, "a.txt")).ino;
    expect(await executor.land(await looked(executor), signal())).toEqual(HOST_STOPPED);
    await until(() => exits === 1);
    expect([existsSync(join(folder, "a.txt")), readdirSync(folder).filter((name) => name.startsWith(".surogate-")).length]).toEqual([false, 2]);
    await executor.stop();
    return { folder, before, was };
  };
  // This computer's tools started again, saying what each recovery comes to.
  const startedAgain = (more: Partial<ToolHostsOptions> = {}) => {
    const told: Recovery[] = [];
    return { executor: threads({ recovered: (recovery) => void told.push(recovery), ...more }), told };
  };
  const putBack = { restored: ["a.txt"], beside: [], lost: [], unread: [] };

  it("puts back, as this computer's tools start again, what a helper killed between an apply's two renames left, before anything else runs in the folder", async () => {
    const { folder, before, was } = await killed();
    const { executor, told } = startedAgain();
    executor.recoverLeft();
    // A chat on the folder asks at once: it is answered once the user's file is back at its name.
    expect(await executor.run(op("read", { key: join(folder, "a.txt"), max_bytes: null }, CHAT), signal())).toEqual({ ok: data64("the user's own\n") });
    expect(told).toEqual([{ folder, state: "begun" }, { folder, state: "found", found: putBack }]);
    expect(executor.recoveries()).toEqual([{ folder, state: "found", found: putBack }]);
    expect(lies(folder)).toEqual(before);
    expect(lstatSync(join(folder, "a.txt")).ino).toBe(was);
    expect(readdirSync(join(base, "data", "landings", keyOf(folder)))).toEqual([]);
  });

  it("says a folder that is gone and leaves what its landings keep as it was, and says a record of theirs it cannot read and does nothing for it", async () => {
    const { folder } = await killed();
    const keeps = join(base, "data", "landings", keyOf(folder));
    renameSync(folder, `${folder} moved`);
    const [moved, kept] = [asItLies(`${folder} moved`), asItLies(keeps)];
    const first = startedAgain();
    first.executor.recoverLeft();
    await until(() => first.told.length === 2);
    expect(first.told[1]).toEqual({ folder, state: "left", why: `the folder ${folder} is not there` });
    expect([asItLies(`${folder} moved`), asItLies(keeps)]).toEqual([moved, kept]);
    await first.executor.stop();
    // Back at its path, with the record of its step damaged: that record is said, and nothing is done in the folder for it.
    renameSync(`${folder} moved`, folder);
    writeFileSync(join(keeps, "s1", "0.json"), "{ damaged");
    const [there, damaged] = [asItLies(folder), asItLies(keeps)];
    const second = startedAgain();
    second.executor.recoverLeft();
    await until(() => second.told.length === 2);
    expect(second.told[1]).toEqual({ folder, state: "found", found: { restored: [], beside: [], lost: [], unread: [["s1", 0, null]] } });
    expect([asItLies(folder), asItLies(keeps)]).toEqual([there, damaged]);
  });

  it("keeps a file the user made at the name since, and puts the one the landing replaced beside it under a name that says so", async () => {
    const { folder, before } = await killed();
    writeFileSync(join(folder, "a.txt"), "made by you since\n");
    const { executor, told } = startedAgain();
    executor.recoverLeft();
    await until(() => told.length === 2);
    expect(told[1]).toEqual({ folder, state: "found", found: { restored: [], beside: [["a.txt", "a (kept by Surogate).txt"]], lost: [], unread: [] } });
    const now = lies(folder);
    expect(now["a (kept by Surogate).txt"]).toEqual(before["a.txt"]);
    expect([readFileSync(join(folder, "a.txt"), "utf8"), Object.keys(now).sort()]).toEqual(["made by you since\n", [...Object.keys(before), "a (kept by Surogate).txt"].sort()]);
  });

  it("makes a recovery that meets a chat holding the folder wait, says so once it waited in vain, and puts the file back once that chat lets the folder go", { timeout: 90_000 }, async () => {
    const { folder, before } = await killed();
    const { executor, told } = startedAgain({ idleMs: 1_500, landWaitMs: 500 });
    // A chat on the folder holds it: its host started before the tools looked for what was cut short.
    expect(await executor.run(op("resolve", { path: "" }, CHAT), signal())).toEqual({ ok: folder });
    executor.recoverLeft();
    await until(() => told.length === 2, 15_000);
    expect(told[1]).toEqual({
      folder, state: "left", why: `Another chat on this computer is working in ${folder}, so what a landing cut short there is not put back yet. It is put back once that one is done`,
    });
    expect(existsSync(join(folder, "a.txt"))).toBe(false);
    // Its host idles out and lets the folder go: the recovery is asked again.
    await until(() => told.length === 4, 15_000);
    expect(told.slice(2)).toEqual([{ folder, state: "begun" }, { folder, state: "found", found: putBack }]);
    expect(lies(folder)).toEqual(before);
  });

  it("never cuts a recovery's put-back where this computer's access ends: it ends first, and then the host", async () => {
    const { folder, before } = await killed();
    const where = join(base, "data", "tmp", `${keyOf(folder)}.recover`);
    const { executor, told } = startedAgain({ spawnHost: upset(where) });
    arm("hold");
    executor.recoverLeft();
    await until(() => existsSync(join(where, "held")), 20_000);
    let over = false;
    const ended = executor.end().then(() => {
      over = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect([over, existsSync(join(folder, "a.txt")), exits]).toEqual([false, false, 1]);
    writeFileSync(join(where, "go"), "");
    await ended;
    expect(told[1]).toEqual({ folder, state: "found", found: putBack });
    expect(lies(folder)).toEqual(before);
  });

  it("puts back what a landing cut short before a thread's open or pickup reads the folder, where no landing's host is on it", async () => {
    const { folder, before } = await killed();
    const { executor, told } = startedAgain();
    const bound = { folder, ...identities.get(folder)!, history: ROOT_A };
    expect(await executor.recoverBefore(bound, signal())).toBeNull();
    expect(told).toEqual([{ folder, state: "begun" }, { folder, state: "found", found: putBack }]);
    expect(lies(folder)).toEqual(before);
    // Nothing is left to put back: the next read starts no host.
    const hosts = spawned.length;
    expect(await executor.recoverBefore(bound, signal())).toBeNull();
    expect([told.length, spawned.length]).toEqual([2, hosts]);
  });

  it("starts no landing's host for a first step cancelled while its thread's copy was opened: a chat on the folder is served past the idle time", { timeout: 90_000 }, async () => {
    const folder = folders[ROOT_A] ?? "";
    const made = copiesOf();
    // Copies whose open answers when the test says.
    const opening: Array<() => void> = [];
    const executor = threads({
      idleMs: 1_000,
      copies: {
        open: async (root, bound, aborted) => {
          await new Promise<void>((go) => void opening.push(go));
          return made.open(root, bound, aborted);
        },
        close: (handle) => made.close(handle),
        ask: forgets,
      },
    });
    const cancel = new AbortController();
    const stepping = executor.land(step("revisions", { paths: [] }), cancel.signal);
    await until(() => opening.length === 1);
    cancel.abort();
    expect(await stepping).toEqual(CANCELLED);
    opening[0]?.();
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    expect(await executor.run(op("resolve", { path: "" }, CHAT), signal())).toEqual({ ok: folder });
    // The chat's is the one host started.
    expect(spawned).toHaveLength(1);
  });
});
