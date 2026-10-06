import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import { forkHost, HOST_STOPPED, type HostProcess, NOT_BOUND } from "../src/hosts/tool-hosts.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { VmExecutor } from "../src/vm/executor.js";
import type { VmOperation } from "../src/vm/manager.js";

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

function vmExecutor(idleMs?: number): VmExecutor {
  const { dev, ino } = statSync(folder);
  executor = new VmExecutor({
    bindingOf: (root) => (root === ROOT ? { folder, dev, ino, boot: BOOT_ID } : undefined),
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    spawnHost: () => {
      const host = forkHost();
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return host;
    },
    ...(idleMs === undefined ? {} : { idleMs }),
    vm: {
      perform: (operation, cancel) => {
        sent.push(operation);
        return guest(operation, cancel);
      },
      teardown: async (root) => {
        torn.push([root, exits]);
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
      { root: ROOT, shared: { path: folder, dev, ino }, kind: "run", args: { command: "true", workdir: null, timeout: 10 } },
      { root: ROOT, shared: { path: folder, dev, ino }, kind: "which", args: { name: "pandoc" } },
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
});
