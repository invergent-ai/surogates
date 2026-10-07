import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { APP_QUIT, type ProcessHandle, RUNNER_GONE } from "../src/guest/processes.js";
import { readRecord, writeRecord } from "../src/hosts/folder-record.js";
import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import { FOLDER_UNAVAILABLE, type NetworkAnswer, type NetworkAsk } from "../src/hosts/messages.js";
import { forkHost, HOST_STOPPED, type HostProcess, type NetworkApprovals, NOT_BOUND } from "../src/hosts/tool-hosts.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { type DeviceStack, stopDevice } from "../src/shell/device-stack.js";
import type { Asker } from "../src/vm/client.js";
import { VmExecutor } from "../src/vm/executor.js";
import type { ProcessesChange, VmOperation } from "../src/vm/manager.js";

const ROOT = "11111111-1111-4111-8111-111111111111";
const UNBOUND = "33333333-3333-4333-8333-333333333333";

let base: string;
let folder: string;
let spawned: HostProcess[];
let exits: number;
let sent: VmOperation[];
// Each root the guest was told to let go, and how many file hosts had exited by then.
let torn: Array<[string, number]>;
// What the VM does with each operation it is sent; by default, a command that says "ran".
let guest: (operation: VmOperation, signal: AbortSignal) => Promise<Outcome>;
// What the VM does with a root's teardown; by default, it answers at once.
let tear: (root: string) => Promise<void>;
// What the VM tells of a root's processes, while the executor listens.
let heard: ((root: string, change: ProcessesChange) => void) | null;
// Run as a file host's ready comes, before its root's host hears it.
let beforeReady: () => void;
// Who the VM asks about a root's destination, while the executor listens.
let asker: Asker | null;
let executor: VmExecutor;

const ran = (output: string): Outcome => ({ ok: { output, returncode: 0, timed_out: false } });
const op = (kind: string, args: Record<string, unknown>, sessionId = ROOT): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId, callingSessionId: sessionId, invocationId: "call", ordinal: 1, kind, args, digest: "d",
});
const signal = () => new AbortController().signal;
async function until(check: () => boolean, ms = 10_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 20))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}

const run = (command = "true") => executor.run(op("run", { command, workdir: null, timeout: 10 }), signal());

function vmExecutor(idleMs?: number, network?: NetworkApprovals): VmExecutor {
  const { dev, ino } = statSync(folder);
  executor = new VmExecutor({
    ...(network ? { network } : {}),
    bindingOf: (root) => (root === ROOT ? { folder, dev, ino, boot: BOOT_ID } : undefined),
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    spawnHost: () => {
      const host = forkHost();
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return { ...host, onMessage: (listener) => host.onMessage((message) => {
        if (message.type === "ready") beforeReady();
        listener(message);
      }) };
    },
    ...(idleMs === undefined ? {} : { idleMs }),
    vm: {
      perform: (operation, cancel) => {
        sent.push(operation);
        return guest(operation, cancel);
      },
      teardown: (root) => {
        torn.push([root, exits]);
        return tear(root);
      },
      onProcesses: (listener) => {
        heard = listener;
        return () => {
          heard = null;
        };
      },
      onAsk: (listener) => {
        asker = listener;
        return () => {
          asker = null;
        };
      },
    },
  });
  return executor;
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "vm-executor-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  spawned = [];
  exits = 0;
  sent = [];
  torn = [];
  guest = async () => ran("ran\n");
  tear = async () => {};
  heard = null;
  asker = null;
  beforeReady = () => {};
});

afterEach(async () => {
  await executor?.stop();
  for (const host of spawned) host.kill();
  rmSync(base, { recursive: true, force: true });
});

describe("the VmExecutor", { timeout: 30_000 }, () => {
  it("runs the file kinds in the root's file host, and the process kinds in the guest, with the folder its binding holds", async () => {
    vmExecutor();
    expect(await executor.run(op("resolve", { path: "a.txt" }), signal())).toEqual({ ok: join(folder, "a.txt") });
    expect(await run()).toEqual(ran("ran\n"));
    expect(await executor.run(op("which", { name: "pandoc" }), signal())).toEqual(ran("ran\n"));
    const { dev, ino } = statSync(folder);
    expect(sent.map(({ root, folder: shared, kind, args }) => ({ root, shared, kind, args }))).toEqual([
      { root: ROOT, shared: { path: folder, dev, ino, boot: BOOT_ID }, kind: "run", args: { command: "true", workdir: null, timeout: 10 } },
      { root: ROOT, shared: { path: folder, dev, ino, boot: BOOT_ID }, kind: "which", args: { name: "pandoc" } },
    ]);
    // One file host for the root, whatever runs where.
    expect(spawned).toHaveLength(1);
  });

  it("answers a bind it cannot confirm, and a root it has no folder for, without the guest", async () => {
    vmExecutor();
    expect(await executor.run(op("bind", { folder, nonce: "n" }), signal())).toEqual(NOT_BOUND);
    expect(await executor.run(op("run", { command: "true", workdir: null, timeout: 10 }, UNBOUND), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(sent).toEqual([]);
  });

  it("looks after a command for the git hooks it left, makes them stop, and says so in its output", async () => {
    vmExecutor();
    // As a command in the guest would, through the share.
    guest = async (operation) => {
      if (operation.kind === "run") {
        mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
        writeFileSync(join(folder, ".git", "hooks", "pre-commit"), "#!/bin/sh\n", { mode: 0o755 });
      }
      return ran("made\n");
    };
    const answer = await run();
    expect(answer).toEqual(ran(`made\n\n${HOOKS_NOTICE}.git/hooks/pre-commit`));
    expect(statSync(join(folder, ".git", "hooks", "pre-commit")).mode & 0o111).toBe(0);
  });

  it("quits within its bound when the guest does not answer a root's teardown, and the VM stops all the same", async () => {
    vmExecutor();
    expect(await run()).toEqual(ran("ran\n"));
    // A manager that is wedged, or a guest waiting on a folder whose mount does not answer.
    tear = () => new Promise(() => {});
    const stopped: string[] = [];
    const begun = performance.now();
    const quit = stopDevice(Promise.resolve({ stop: () => executor.stop() } as DeviceStack), { stop: async () => void stopped.push("vm") });
    expect(await Promise.race([quit.then(() => "quit"), new Promise((resolve) => setTimeout(() => resolve("still quitting"), 9_000))])).toBe("quit");
    // The host's own bound on its stop: 5 s.
    expect(performance.now() - begun).toBeLessThan(7_000);
    expect([torn.map(([root]) => root), stopped, exits]).toEqual([[ROOT], ["vm"], 1]);
  });

  it("keeps looking after a command for the hooks what it left running in the guest writes later", async () => {
    vmExecutor();
    const hook = join(folder, ".git", "hooks", "pre-commit");
    // A leftover of the command's, still running in the guest once the command has answered.
    guest = async () => {
      setTimeout(() => {
        mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
        writeFileSync(hook, "#!/bin/sh\n", { mode: 0o755 });
      }, 300);
      return ran("ran\n");
    };
    expect(await run()).toEqual(ran("ran\n"));
    const answered = performance.now();
    await until(() => {
      try {
        return (statSync(hook).mode & 0o111) === 0;
      } catch {
        return false;
      }
    }, 8_000);
    // Within the host's look every 5 s.
    expect(performance.now() - answered).toBeLessThan(6_500);
  });

  it.skipIf(process.getuid?.() === 0)("refuses a command while the hook guard cannot see the whole folder, and never sends it", async () => {
    vmExecutor();
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0o000);
    try {
      expect(await run()).toMatchObject({ error: { type: "sandbox", message: expect.stringContaining("Blocked: the computer cannot read locked") } });
      // which runs no command: the guard is not asked.
      expect(await executor.run(op("which", { name: "sh" }), signal())).toEqual(ran("ran\n"));
      expect(sent.map((operation) => operation.kind)).toEqual(["which"]);
    } finally {
      chmodSync(locked, 0o700);
    }
  });

  it.skipIf(process.getuid?.() === 0)("refuses a background process's start, and input to one, while the hook guard cannot see the whole folder", async () => {
    vmExecutor();
    const locked = join(folder, "locked");
    mkdirSync(locked);
    chmodSync(locked, 0o000);
    try {
      for (const [kind, args] of [["start", { command: "make watch" }], ["write_stdin", { session_id: "proc_000000000001", data: "rm -rf .git\n" }]] as const) {
        expect(await executor.run(op(kind, args), signal())).toMatchObject({
          error: { type: "sandbox", message: expect.stringContaining("Blocked: the computer cannot read locked") },
        });
      }
      // Reading what a process said runs nothing: the guard is not asked.
      expect(await executor.run(op("poll", { session_id: "proc_000000000001" }), signal())).toEqual(ran("ran\n"));
      expect(sent.map((operation) => operation.kind)).toEqual(["poll"]);
    } finally {
      chmodSync(locked, 0o700);
    }
  });

  it("keeps the root's file host, and with it the folder, while a command runs past the host's idle time", async () => {
    vmExecutor(200);
    guest = async () => {
      await new Promise((resolve) => setTimeout(resolve, 800));
      return ran("slow\n");
    };
    expect(await run()).toEqual(ran("slow\n"));
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folder });
    expect(spawned).toHaveLength(1);
  });

  it("answers a command whose file host stops while it runs as stopped by the tool host", async () => {
    vmExecutor();
    guest = async () => {
      spawned[0]?.kill();
      await new Promise((resolve) => setTimeout(resolve, 300));
      return ran("too late\n");
    };
    expect(await run()).toEqual(HOST_STOPPED);
  });

  it("answers a cancel while the root's file host starts, and sends nothing to the guest", async () => {
    vmExecutor();
    const cancel = new AbortController();
    const answer = executor.run(op("run", { command: "true", workdir: null, timeout: 10 }), cancel.signal);
    cancel.abort();
    expect(await answer).toMatchObject({ error: { type: "cancelled" } });
    expect(sent).toEqual([]);
  });

  it("keeps the root's file host, and with it the folder, while a process of the root's lives in the guest", async () => {
    vmExecutor(200);
    const handle: ProcessHandle = { id: "proc_000000000001", command: "npm run dev", cwd: folder, task_id: null, started_at: Date.now() / 1000 };
    expect(await executor.run(op("start", { command: "npm run dev" }), signal())).toEqual(ran("ran\n"));
    heard?.(ROOT, { handles: [handle], live: 1 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect([exits, torn]).toEqual([0, []]);
    heard?.(ROOT, { handles: [{ ...handle, ended: { exit_code: 0, output: "", note: null } }], live: 0 });
    await until(() => exits === 1);
    expect(torn).toEqual([[ROOT, 0]]);
  });

  it("looks for the hooks a background process leaves every 5 s while it lives, not just after its start", async () => {
    vmExecutor();
    const hook = join(folder, ".git", "hooks", "pre-commit");
    const started = { ok: { session_id: "proc_000000000001", pid: 7 } };
    // The process writes a hook once its start has been answered, as a dev server's setup step might.
    guest = async () => {
      heard?.(ROOT, { handles: [{ id: "proc_000000000001", command: "make", cwd: folder, task_id: null, started_at: Date.now() / 1000 }], live: 1 });
      setTimeout(() => {
        mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
        writeFileSync(hook, "#!/bin/sh\n", { mode: 0o755 });
      }, 300);
      return started;
    };
    expect(await executor.run(op("start", { command: "make" }), signal())).toEqual(started);
    const answered = performance.now();
    await until(() => {
      try {
        return (statSync(hook).mode & 0o111) === 0;
      } catch {
        return false;
      }
    }, 8_000);
    expect(performance.now() - answered).toBeLessThan(6_500);
  });

  it("keeps the handles in the folder's record, gives them to each operation in the guest, and ends those still running when the guest goes", async () => {
    vmExecutor();
    const { dev, ino } = statSync(folder);
    const record = join(base, "data", "folders", `${dev}-${ino}.json`);
    const handle: ProcessHandle = { id: "proc_000000000001", command: "sleep 9", cwd: folder, task_id: "t", started_at: Date.now() / 1000 };
    expect(await run()).toEqual(ran("ran\n"));
    expect(sent[0]?.ended).toEqual([]);
    heard?.(ROOT, { handles: [handle], live: 1 });
    await until(() => readRecord(record)?.processes.length === 1);
    expect(await run()).toEqual(ran("ran\n"));
    expect(sent[1]?.ended).toEqual([handle]);
    heard?.(ROOT, { gone: true });
    const lost = { ...handle, ended: { exit_code: null, output: "", note: RUNNER_GONE } };
    await until(() => readRecord(record)?.processes[0]?.ended !== undefined);
    expect(readRecord(record)?.processes).toEqual([lost]);
    expect(await run()).toEqual(ran("ran\n"));
    expect(sent[2]?.ended).toEqual([lost]);
  });

  it("gives the guest the handles the folder's record kept from before the app quit, each ended as the app quit", async () => {
    const { dev, ino } = statSync(folder);
    const handle: ProcessHandle = { id: "proc_000000000001", command: "sleep 9", cwd: folder, task_id: "t", started_at: Date.now() / 1000 };
    writeRecord(join(base, "data", "folders", `${dev}-${ino}.json`), { state: "stopped", present: [], hooks: null, processes: [handle] });
    vmExecutor();
    expect(await executor.run(op("poll", { session_id: handle.id }), signal())).toEqual(ran("ran\n"));
    expect(sent[0]?.ended).toEqual([{ ...handle, ended: { exit_code: null, output: "", note: APP_QUIT } }]);
  });

  it("gives the guest what a root's file host last kept once it let the folder go, its live processes ended as the app quit, whatever the VM tells of the root after", async () => {
    vmExecutor();
    const handle: ProcessHandle = { id: "proc_000000000001", command: "sleep 9", cwd: folder, task_id: "t", started_at: Date.now() / 1000 };
    expect(await run()).toEqual(ran("ran\n"));
    heard?.(ROOT, { handles: [handle], live: 1 });
    // A manager killed for a teardown it did not answer in time tells the root gone, and so does one that exits later.
    tear = async (root) => heard?.(root, { gone: true });
    await executor.end();
    heard?.(ROOT, { gone: true });
    // The teardown ended the process, and told nothing: the next file host's registry says how.
    expect(await run()).toEqual(ran("ran\n"));
    expect(sent.at(-1)?.ended).toEqual([{ ...handle, ended: { exit_code: null, output: "", note: APP_QUIT } }]);
  });

  it("lets the root's file host go once the guest that ran its live processes goes, and keeps them ended with the guest", async () => {
    vmExecutor(200);
    const { dev, ino } = statSync(folder);
    const record = join(base, "data", "folders", `${dev}-${ino}.json`);
    const handle: ProcessHandle = { id: "proc_000000000001", command: "npm run dev", cwd: folder, task_id: null, started_at: Date.now() / 1000 };
    expect(await executor.run(op("start", { command: "npm run dev" }), signal())).toEqual(ran("ran\n"));
    heard?.(ROOT, { handles: [handle], live: 1 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(exits).toBe(0);
    heard?.(ROOT, { gone: true });
    // Within its idle time: nothing of the root's is left alive.
    await until(() => exits === 1, 2_000);
    expect(readRecord(record)?.processes).toEqual([{ ...handle, ended: { exit_code: null, output: "", note: RUNNER_GONE } }]);
  });

  it("keeps the folder's record of what the root ran before, whatever the VM tells of the root before its file host is ready", async () => {
    const { dev, ino } = statSync(folder);
    const record = join(base, "data", "folders", `${dev}-${ino}.json`);
    const handle: ProcessHandle = { id: "proc_000000000001", command: "sleep 9", cwd: folder, task_id: "t", started_at: Date.now() / 1000 };
    const quit = { ...handle, ended: { exit_code: null, output: "", note: APP_QUIT } };
    writeRecord(record, { state: "stopped", present: [], hooks: null, processes: [handle] });
    vmExecutor(200);
    // Crossing the file host's ready, once its record is read: a change of the root's from the guest, then a gone, when it goes.
    beforeReady = () => {
      heard?.(ROOT, { handles: [handle], live: 1 });
      heard?.(ROOT, { gone: true });
    };
    expect(await run()).toEqual(ran("ran\n"));
    expect(sent[0]?.ended).toEqual([quit]);
    // Idle, the host lets the folder go: the record is as the host found it, so the next answers for the process as before.
    await until(() => exits === 1, 2_000);
    expect(readRecord(record)?.processes).toEqual([quit]);
  });

  it("hears the guest only until it stops", async () => {
    vmExecutor();
    expect(heard).not.toBeNull();
    await executor.stop();
    expect(heard).toBeNull();
  });

  it("lets the guest's root go before the root's file host lets its folder go, and when the device's access ends", async () => {
    vmExecutor(200);
    expect(await run()).toEqual(ran("ran\n"));
    // Idle: the guest's root first, while the file host still holds the folder, then the host.
    await until(() => exits === 1);
    expect(torn).toEqual([[ROOT, 0]]);
    expect(await run()).toEqual(ran("ran\n"));
    await executor.end();
    expect(torn).toEqual([[ROOT, 0], [ROOT, 1]]);
  });

  describe("a destination a chat's command in the guest asks for", () => {
    const SITE: NetworkAsk = { host: "example.com", port: 443, privateNetwork: false };

    it("is the chat's approvals' to decide while something of the chat runs, and denied unasked once nothing does", async () => {
      const asked: Array<[string, NetworkAsk]> = [];
      vmExecutor(undefined, { granted: () => [], askNetwork: async (root, request) => (asked.push([root, request]), "allow_session") });
      let release = () => {};
      guest = (operation) => (operation.kind === "run" ? new Promise((resolve) => {
        release = () => resolve(ran("ran\n"));
      }) : Promise.resolve(ran("")));
      const running = run("curl https://example.com");
      await until(() => sent.length === 1);
      expect(await asker?.(ROOT, SITE)).toBe("allow_session");
      // Another device's chat is not this one's to answer.
      expect(asker?.(UNBOUND, SITE)).toBeNull();
      release();
      await running;
      expect(await asker?.(ROOT, SITE)).toBe("deny");
      expect(asked).toEqual([[ROOT, SITE]]);
    });

    it("dismisses the chat's open prompt once nothing of the chat runs, and denies with it", async () => {
      let dismissed = false;
      vmExecutor(undefined, {
        granted: () => [],
        askNetwork: (_root, _request, signal) => new Promise<NetworkAnswer>((resolve) => signal.addEventListener("abort", () => {
          dismissed = true;
          resolve("deny");
        })),
      });
      let release = () => {};
      guest = () => new Promise((resolve) => {
        release = () => resolve(ran("ran\n"));
      });
      const running = run("curl https://example.com");
      await until(() => sent.length === 1);
      const answer = asker?.(ROOT, SITE);
      release();
      await running;
      expect(await answer).toBe("deny");
      expect(dismissed).toBe(true);
    });

    it("is denied for a chat without approvals to ask, and listened for only until the executor stops", async () => {
      vmExecutor();
      guest = () => new Promise(() => {});
      void run("curl https://example.com");
      await until(() => sent.length === 1);
      expect(await asker?.(ROOT, SITE)).toBe("deny");
      await executor.stop();
      expect(asker).toBeNull();
    });
  });
});
